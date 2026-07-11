# ADR 0044 — Asymmetric attestation substrate (DSSE envelope + pluggable schemes)

- **Status:** Accepted (epic `brilliant-curly-songbird` / 2fd4)
- **Date:** 2026-07-11

## Context

rebar signs its attestations — plan-review results at claim time, completion-verifier
verdicts at close time — with a **symmetric HMAC** keyed on a per-environment secret
(`src/rebar/signing.py`; `.signing-key` / `REBAR_SIGNING_KEY`). That model is exactly
right for its job: it proves a verdict was produced *in this environment* and hasn't been
transplanted. But two upcoming capabilities need something HMAC cannot give:

- **Per-person authorship signing** (the identity epic) — "who attested this?" answered by
  a signature only that person could have produced.
- **Third-party-verifiable operation certificates** (the op-cert epic) — a certificate
  anyone can verify offline, that no one but the signer could forge.

Both need **asymmetric** signatures (verify-by-anyone, forge-by-none) over attestation
bytes, verified offline. Building that signing layer twice would duplicate
security-critical code and worsen a namespace collision already forming on the
`SIGNATURE` event type / `signature` / `key_id` fields. So we build **one** hardened,
standards-based signing substrate that both consumer epics import.

The alternative — a full crypto library such as `securesystemslib` — was rejected: it has
**no SSH signer** (so it would not cover the authorship path) and pulls `cryptography`
(heavy) for Ed25519. rebar core stays stdlib-only.

## Decision

Reuse established standards rather than hand-roll crypto. The substrate lives in the new
`src/rebar/attest/` package and has four parts.

### 1. DSSE envelope + PAE (`attest/dsse.py`)

Signatures cover **DSSE (Dead Simple Signing Envelope) Pre-Authentication Encoding**
bytes, not a re-serialized JSON form:

```
PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body
```

(`SP` = one ASCII space; `LEN` = ASCII-decimal **byte** length). PAE has two properties we
rely on:

- It signs **exact opaque bytes**. Verification never re-serializes parsed JSON, so the
  JSON canonicalization / duplicate-key attack class is closed. `decode()` returns the
  byte-exact stored `payload`; `Envelope.pae()` computes PAE over those exact bytes.
- It carries **no in-band `alg`** the verifier trusts — closing the JWT `alg`-confusion
  class (RFC 8725).

The `Envelope` (`{payload, payloadType, signatures:[{keyid, sig}]}`) is JSON with the
`payload` and each `sig` standard-base64 (RFC 4648 §4) encoded. DSSE is the
Sigstore / in-toto / SLSA standard; the encoder is ~30 lines, offline, scheme-agnostic.

### 2. Pluggable scheme registry + per-kind policy table (`attest/registry.py`)

A **static, source-pinned** policy table maps each attestation `kind` to a
`Policy(scheme, namespace)`:

```python
def verify(kind, envelope, trust_root) -> Verdict:
    policy = POLICY[kind]                 # KeyError → fail closed (unknown_kind)
    scheme = _SCHEMES[policy.scheme]      # missing → fail closed (unknown_scheme)
    return scheme.verify(envelope.pae(), envelope.signatures, policy.namespace, trust_root)
```

**Domain separation is the whole point.** The scheme is selected **only** from
`POLICY[kind].scheme` — *never* from the envelope's `keyid` or signature contents. An
attacker who controls the record cannot influence which algorithm/scheme the verifier
uses. `POLICY` is a hardcoded module-level dict, deliberately **not** loaded from config or
env, because attacker/config control over scheme selection is exactly the threat the table
removes. Unknown kind or unknown scheme fails **closed** (a non-verified `Verdict`, never
an exception a caller might treat as a pass).

`trust_root` (which keys to trust) is a caller/deployment concern, supplied at the call
site — not pinned in the table. The table pins the security-critical *dispatch*
(`scheme` + `namespace`); trust material is passed in.

### 3. SSHSIG scheme (`attest/sshsig.py`)

The first asymmetric scheme: sign/verify via OpenSSH `ssh-keygen -Y` (stdlib
`subprocess`, **no new core dependency**), reusing the reference implementation and
developers' existing SSH keys, verified offline against an `allowed_signers`-format trust
input. `verify` uses **argv arrays** (never a shell), feeds the payload on stdin, and
branches **only on the process exit code**. Fail-closed behavior was confirmed by direct
experiment (OpenSSH 10.2): tampered bytes, wrong namespace, unknown principal, a
substituted key, and an expired validity window all exit non-zero. `ssh-keygen` >= 8.9 is
required (the floor for `allowed_signers` validity intervals) and its absence fails closed
with a clear error — never a silent pass. `-Y check-novalidate` (structure-only) is never
used for authorization.

### 4. HMAC as a registered legacy scheme (`attest/hmac_legacy.py`)

The existing HMAC-SHA256 primitive is registered as a **first-class registry scheme**
keyed by its algorithm id, and the legacy kinds (`plan-review`, `completion-verifier`) are
pinned to it. This is the **expand** phase: the substrate now *knows* HMAC so both
consumer epics reference one registry.

Scope boundary: existing legacy attestations (a hex HMAC over the canonical
`{v,algorithm,ticket_id,manifest}` payload) are **not** rerouted through the registry —
they keep verifying unchanged through the untouched `src/rebar/signing.py`. `HmacScheme`
here is the forward-looking HMAC-**over-DSSE-PAE** scheme. Re-enveloping old records and
the HMAC→asymmetric cutover are the op-cert epic's **contract** phase, out of scope here.

## Consequences

- **One substrate, imported twice.** The identity and op-cert epics get a hardened,
  standards-based signing layer instead of two divergent stacks. No consumer needs its own
  crypto.
- **No new core runtime dependency.** SSHSIG rides on `ssh-keygen`; DSSE/PAE and the
  registry are pure stdlib.
- **Offline, standards-based verification.** Signatures cover DSSE-PAE bytes; verification
  never re-serializes JSON and never trusts an in-band algorithm.
- **No behavior regression.** `signing.py` is untouched; existing plan-review /
  completion-verifier attestations still verify (HMAC as a registered legacy scheme).
- **Signing entry points are deferred to consumers.** The substrate provides the unified
  **verify** path (verify-by-anyone is the shared need) plus per-scheme sign helpers
  (`dsse.encode` for enveloping, `sshsig.sign` for SSHSIG). A unified `sign(kind, body)`
  is intentionally not added here, because producing a signature needs key material whose
  location is scheme- and deployment-specific (the env key for HMAC, a private key path
  for SSHSIG) — that entry point belongs to the consumer that owns the key.

## Prior art

DSSE protocol v1.0.0 (secure-systems-lab/dsse); in-toto attestation; OpenSSH
`PROTOCOL.sshsig` + `allowed_signers`; git SSH commit signing
(`gpg.ssh.allowedSignersFile`); RFC 8725 (JWT BCP); RFC 8785 (JCS, considered and rejected
in favour of DSSE PAE).
