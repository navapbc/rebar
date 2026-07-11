# The attestation substrate (`rebar.attest`)

A hardened, standards-based signing substrate for rebar attestations: a **DSSE envelope**,
a **pluggable scheme registry** with a **per-kind policy table**, an **SSHSIG** asymmetric
scheme, and the existing **HMAC** registered as a legacy scheme. It is the shared signing
layer the identity and operation-certificate epics build on. Design rationale and the
threat model are in [ADR 0044](adr/0044-asymmetric-attestation-substrate.md); this page is
the developer reference for the API.

## The pieces

| Module | Provides |
|--------|----------|
| `rebar.attest.dsse`        | `pae()`, `encode()`/`decode()`, `Envelope`, `Signature` |
| `rebar.attest.registry`    | `verify(kind, envelope, trust_root)`, `Policy`, `Verdict`, `Scheme`, `register_scheme()`, `resolve()`, `POLICY` |
| `rebar.attest.sshsig`      | `SshsigScheme`, `sign()`, `ssh_keygen_version()`, `ensure_available()` |
| `rebar.attest.hmac_legacy` | `HmacScheme`, `register_legacy_schemes()` |

Importing `rebar.attest` registers the built-in schemes (HMAC-legacy and SSHSIG) and pins
the legacy-kind policy, so the registry is ready to use on import.

## DSSE envelope + PAE

Signatures cover **Pre-Authentication Encoding** bytes, not a re-serialized JSON form:

```python
from rebar.attest import dsse

pae_bytes = dsse.pae("application/vnd.rebar.attest+json", body_bytes)
# -> b'DSSEv1 <len(type)> <type> <len(body)> <body>'
```

Envelope encode/decode round-trips the exact opaque body (base64 in the JSON `payload`
field); `decode()` returns byte-identical bytes even for non-canonical / duplicate-key
JSON, so verification never re-serializes:

```python
text = dsse.encode(payload_type, body_bytes, [{"keyid": "id", "sig": sig_bytes}])
env  = dsse.decode(text)          # Envelope(payload_type, payload, signatures)
env.pae()                         # PAE over the exact stored payload bytes
```

## Verifying an attestation

`verify` resolves the kind's pinned policy and dispatches to that scheme. **The scheme is
chosen from the policy table, never from the envelope** — this is the domain-separation /
anti-`alg`-confusion property (RFC 8725).

```python
from rebar.attest import registry

verdict = registry.verify(kind, envelope, trust_root)
# Verdict(verified: bool, verdict: str, reason: str)
#   verdict ∈ {"certified", "mismatch", "unknown_kind", "unknown_scheme", "invalid",
#              "foreign_key", "unavailable", ...}
if verdict.verified:
    ...  # certified
```

- `kind` selects `Policy(scheme, namespace)` from `registry.POLICY`. An **unknown kind** or
  an **unknown policy scheme** fails **closed** (a non-verified `Verdict`, never an
  exception).
- `trust_root` is the scheme-specific trust anchor supplied by the caller — the
  `allowed_signers` content for SSHSIG, the HMAC key for the HMAC scheme. *Which keys to
  trust* is a deployment concern; it is **not** pinned in the policy table.
- The scheme receives the envelope's `pae()` bytes, its signatures, and the
  **policy-pinned** `namespace` (domain separation), plus `trust_root`.

## Signing an attestation for a kind (`sign(kind, body)`)

Producing a signed attestation for a `kind` is the mirror of `verify`: look up the kind's
pinned `Policy(scheme, namespace)`, produce a signature with **that scheme's signer** over
the DSSE-PAE bytes, and wrap it in an envelope. There is deliberately **no single
`sign(kind, body)` function** in the substrate, because producing a signature needs *key
material* whose location is scheme- and deployment-specific — the environment HMAC key, or
a private SSH key path — that the substrate cannot and should not resolve on the caller's
behalf (see ADR 0044, *Consequences*). The **verify** side is unified (verify-by-anyone is
the shared need); the **sign** side is assembled by the caller from the kind's scheme
signer. The pattern:

```python
from rebar.attest import dsse, registry, sshsig

def sign_for_kind(kind: str, body: bytes, *, key_path: str) -> str:
    policy = registry.resolve(kind)                 # {scheme, namespace}
    pae_bytes = dsse.pae("application/vnd.rebar.attest+json", body)
    if policy.scheme == "sshsig":
        sig = sshsig.sign(pae_bytes, key_path, policy.namespace)   # SSHSIG signer
        keyid = "attester@example.com"              # the principal
    # (HMAC-kind signing uses the env key over pae_bytes; see docs/manifest-signing.md)
    return dsse.encode("application/vnd.rebar.attest+json", body, [{"keyid": keyid, "sig": sig}])

envelope_text = sign_for_kind("authorship", body, key_path="~/.ssh/id_ed25519")
verdict = registry.verify("authorship", dsse.decode(envelope_text), allowed_signers)
```

The namespace used to sign is the **policy-pinned** namespace for the kind — the same one
`verify` hands the scheme — so signing and verifying share one domain-separation string
and never drift.

## Registering a new kind (`{scheme, namespace, trust_root}`)

To make a new attestation `kind` verifiable, pin it in the policy table to an existing
scheme (or register a new scheme first). This is how a consumer epic (e.g. authorship)
wires a kind to SSHSIG:

```python
from rebar.attest import registry

# 1. (optional) register a scheme, if not a built-in. A Scheme implements:
#    name: str
#    verify(pae_bytes, signatures, namespace, trust_root) -> registry.Verdict
registry.register_scheme(MyScheme())

# 2. pin the kind → {scheme, namespace}. The namespace is the domain-separation
#    string handed to the scheme; trust_root is supplied per-call, not pinned.
registry.POLICY["authorship"] = registry.Policy(
    scheme="sshsig",
    namespace="rebar-authorship",
)

# 3. verify — the caller supplies trust_root (here, allowed_signers content):
verdict = registry.verify("authorship", envelope, allowed_signers_text)
```

Because scheme selection comes only from `POLICY[kind].scheme`, adding a kind cannot be
subverted by envelope contents.

## The built-in schemes

### SSHSIG (`rebar.attest.sshsig`)

Asymmetric signing via OpenSSH `ssh-keygen -Y` (stdlib subprocess, no new dependency).

```python
from rebar.attest import dsse, sshsig, registry

sig = sshsig.sign(pae_bytes, key_path="~/.ssh/id_ed25519", namespace="rebar-authorship")
env = dsse.decode(dsse.encode(payload_type, body, [{"keyid": "alice@example.com", "sig": sig}]))

# trust_root is allowed_signers content; keyid is the principal.
verdict = sshsig.SshsigScheme().verify(env.pae(), env.signatures, "rebar-authorship", allowed_signers)
```

Fails closed on tampered bytes, wrong namespace, unknown principal, a substituted key, an
expired validity window (`valid-before` in `allowed_signers`), and when `ssh-keygen` is
absent or `< 8.9`. `ensure_available()` raises `SshKeygenUnavailable` on an unusable
toolchain.

### HMAC legacy (`rebar.attest.hmac_legacy`)

The existing HMAC-SHA256 primitive as a forward-looking HMAC-**over-DSSE-PAE** registry
scheme, keyed `HMAC-SHA256`, with the legacy kinds (`plan-review`, `completion-verifier`)
pinned to it (namespace `rebar-attest-hmac`). `trust_root` is the HMAC key.

> **Scope note.** Existing legacy attestations (a hex HMAC over the canonical
> `{v,algorithm,ticket_id,manifest}` payload) still verify **unchanged** through
> `rebar.signing` — they are not rerouted through the registry. Re-enveloping old records
> is the operation-certificate epic's contract phase. See `docs/manifest-signing.md` for
> the legacy HMAC signing path.
