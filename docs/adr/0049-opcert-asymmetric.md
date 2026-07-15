# ADR 0049 — Operation certificates: asymmetric, environment-attributed, optionally required

- **Status:** Accepted
- **Date:** 2026-07-14
- **Relates to:** ADR 0044 (asymmetric attestation substrate), ADR 0045 (authenticated
  identity), ADR 0046 (security posture and accepted limitations), ADR 0043
  (operator-attested completion evidence). This ADR is **op-cert-scoped** and defers to
  ADR 0046 for the cross-cutting accepted limitations (L1 anti-rollback, L2 no-transparency-log,
  L4 single-environment/no-threshold).
- **Epic:** `sonic-columned-sturgeon` / `6d0d-7580-e18f-48c3` (operation certificates —
  asymmetric + environment-attributed + per-project required-environment).

## Context

rebar issues two **operation certificates** — the plan-review verdict (at claim) and the
completion-verifier verdict (at close). Before this epic both were **local self-attestations**:
signed with a per-clone **symmetric HMAC secret** (`src/rebar/signing.py`) and verifiable only by
the clone that holds the forge-capable secret. Nothing at merge (Gerrit `Verified` / CI)
re-verified them, so they were **local discipline, not enforceable controls**, and no third party
could verify one.

This ADR records the **settled** model that closes that gap. It is the design that emerged from the
epic's two design-resolution rounds (the trust-config/era-validity protocol and the round-6
producer-signing/compute-runtime convergence), including the **authoritative era anchor**
correction. It rests on the shipped attestation substrate (ADR 0044) and reuses the identity epic's
merge-gate, merged-log ledger, and era-validity *rule/verdict* (ADR 0045) — while keeping the two
kinds independent in actor and threat model.

## Decision

### 1. Environment-attribution model (orthogonal to authorship)

Operation certificates are **asymmetric** signatures in a dedicated namespace, **`rebar.opcert.v1`**,
registered as a new kind in the foundation's per-kind `POLICY` table (`src/rebar/attest/registry.py`)
alongside HMAC-legacy and the identity kind. An op-cert is **attributed to the signing environment**
and is **verify-by-anyone** against that environment's public key — no shared secret is needed to
verify.

This restores a clean **two-actor split**: an op-cert attributes an **environment** (a dev machine
or a shared server are both "environments"); an authorship attestation (`rebar.authorship.v1`, ADR
0045) attributes a **person**. The two kinds are **orthogonal in actor and threat model** — different
key, different trust root, different namespace — so a signature is never valid across kinds. This
independence is a *dependency-direction* guarantee (op-certs sign with environment keys, never
person/identity keys), **not** a claim that no code is shared: the op-cert kind deliberately
**reuses** the identity epic's delivered machinery (the `verify-identity` merge-gate CI job, the
merged-log ledger, and the era-validity *rule* and `key_not_valid_at_era` verdict) rather than
building parallel copies.

### 2. Low-security default vs high-security posture

- **Low-security (default).** Any/local certificate is a **process record, not a control** — exactly
  today's HMAC semantics, now verifiable-by-anyone against the environment's public key. There is no
  `verify.require_environment` set and no `.rebar/trusted_environments.yaml` present. The default,
  offline path **never requires the network and never wedges** an operation.
- **High-security (opt-in).** A project pins a **required trusted environment** and the **merge gate**
  (Gerrit/CI) enforces that the required environment's completion-verifier certificate exists and
  verifies over the merged log. Turning it off (absent trust file / no `require_environment`) reverts
  to local behavior — the opt-in defaults off, which is also the rollback path.

### 3. Authoritative-state-fetch integrity requirement (load-bearing)

A trusted certificate attests that the **trusted environment fetched the authoritative state itself**
— ticket state and code-at-commit from the authoritative remotes — and ran the gate on **that**
state. **Client-provided inputs cannot forge a pass** (SLSA-style non-falsifiable provenance): the
developer who wants to pass cannot doctor the inputs the verdict is computed over. Concretely, the
verify side does not trust the record's own echoed inputs — it **recomputes** the
`material_fingerprint` from the authoritative ticket state its own merged-log walk produced and
requires the signed subject to bind that value; the `merged_log_commit` (which only the producer can
know) is **constrained**, not recomputed — read only from the signature-verified payload and required
to resolve to a real ancestor of the fetched `main` tip and to satisfy the era/enforce-since floors,
failing closed on any unresolvable value.

Per **ADR 0046** (and SLSA's own caveat), a valid op-cert attests that **state came from the trusted
fetcher and the process ran without blocking issues — NOT that the underlying work is correct** or
defect-free. Correctness assurance comes from the review gates and human review, not the signature.

### 4. Out-of-band trust root

Trusted-environment public keys are pinned in **`.rebar/trusted_environments.yaml`** at the repo
root. This file lives **out-of-band on the Gerrit-gated, CODEOWNERS-protected code branch (`main`)**
— **never** on the auto-pushed `tickets` branch (which any writer can advance). An absent file means
"no required environment" (the low-security default). Keys are OpenSSH `authorized-keys`-form
Ed25519 public keys, and each key records its era boundaries in the **tickets-branch log-position**
form: **`added_at_log_position`** / **`revoked_at_log_position`** (an operator stamps the current
tickets tip at add/revoke time; the `rebar trusted-env` helper does this). This is deliberately
unlike the identity epic's *in-band* TOFU authorship keys — the op-cert's high-authority trust root
must be established out-of-band (ADR 0046 L3 records why op-cert cannot inherit authorship's TOFU
model wholesale).

### 5. Merge gate + authoritative era anchor

`rebar verify-opcert` verifies the required-environment completion-verifier certificate over the
**merged log**, extending the shipped `verify-identity.yaml` merge gate. Verification is **against
the out-of-band-pinned public key**, not the certificate's self-claimed (unauthenticated) DSSE
`keyid` hint. Certificates bind `{ticket id, material fingerprint, merged-log position}` so a cert
cannot be replayed onto another ticket or stale state.

**Key era-validity is judged at the certificate's storage anchor `S`** — the introducing commit of
the envelope-bearing `SIGNATURE` event, resolved by the gate's **own** merged-log walk (the same
fail-closed resolver used for the close anchor; an unresolvable/compacted anchor fails closed). This
is the corrected model (it **supersedes** the earlier "era at the cert's self-chosen
`merged_log_commit`" rule): the storage anchor is attacker-uninfluenceable in the backward direction
because the shared tickets branch is append-only (the ADR 0046 L1 bounding control) — a cert can be
stored late, never backdated early. Comparison is plain commit ancestry (`git merge-base
--is-ancestor`) between `S` and the pinned key's log-position era boundaries — same DAG, no cross-DAG
inference.

- **Revocation semantics.** A **revoked key with no grandfather pin fails ALL its certs** — the
  compromise **kill-switch** (re-certification under a new key is required). A **revoked key WITH a
  pin keeps certs stored at/before the pin valid** — routine rotation, history intact.
- The bound **`merged_log_commit`** (on the `main` DAG) is demoted to a **code-state claim only** —
  "the gate ran against this `main` commit" — still constrained to be an ancestor of the gated HEAD
  and to satisfy the enforce-since floor, but it **carries no key-validity semantics** and is sourced
  only from the signed payload.

### 6. Uniform producer signing (round-6 resolution)

**Every environment signs its own verdicts.** The gate producers themselves mint `rebar.opcert.v1`
certs — the plan-review sign step (`src/rebar/llm/plan_review/attest.py`) and the close-gate sign
step (`src/rebar/_commands/transition_close.py`) are repointed from `signing.sign_manifest` (HMAC) to
`signing.sign_opcert_manifest`. Each environment holds a **passphrase-free, auto-generated Ed25519
key** at **`<tracker>/.opcert-key`** (+ `.opcert-key.pub`, mode `0600`, git-ignored) — generated on
first use exactly as the HMAC `.signing-key` it replaces. **The trusted server is not a special
signer**: it is an ordinary environment whose key happens to be pinned out-of-band and which fetches
authoritative state. There is **exactly one signature per verdict** — no double-signing. If
`ssh-keygen` (OpenSSH ≥ 8.9) is unavailable, signing degrades to the in-band `{signed: false, error:
…}` record so a local op never wedges (a project gate that *requires* a signature then blocks with a
clear install-OpenSSH remediation).

### 7. Per-kind native verdict vocabularies + transport/verdict split

Verdict vocabularies stay **native per kind** — there is no unified enum:

- **plan-review:** `PASS | BLOCK | INDETERMINATE`
- **completion-verifier:** `PASS | FAIL` (with `LLMError` propagation)

The remote job API separates **transport status** from **gate verdict**:
`{status: "completed"|"error", kind, verdict: <kind's native enum>|null, envelope: <DSSE>|null,
material_fingerprint, merged_log_commit, error: {class, message}|null}`. An op-cert `envelope` is
present **only** on a `PASS` verdict; a raised `LLMError`/`LLMUnavailableError` maps to `status:
"error"` with a null verdict.

### 8. Signer identity is deployment config; consumer/client keys are separate

The **signer's identity is deployment configuration** — `REBAR_OPCERT_ENV_ID` (env var / task
definition), defaulting to the environment's existing `env_id`. There is deliberately **no
repo-config `env_id` key**: the repo under verification must not get to choose who signs it.
Consumer-side policy is two existing `VerifyConfig` keys — **`verify.require_environment`** and
**`verify.opcert_enforce_since`** — and client-side remote routing adds exactly one new repo key,
**`verify.opcert_remote_url`** (`str | None`; absent = fully local, nothing remote ever required).

### 9. Cost-verified runtime choice

The trusted service **rides the existing `rebar-gerrit` VM as a docker-compose service** (behind the
box's nginx, alongside the review bot), fronted by an **AWS API Gateway HTTP API with a SigV4/IAM
authorizer** restricted to a named admin role. It exposes an **async job API** (`POST /opcert/jobs`
→ `202 {job_id}`; `GET /opcert/jobs/{id}` → status/result); job state is in-memory (a restart loses
queued jobs — clients re-submit, since cert minting is idempotent). The Ed25519 signing key lives in
**SSM Parameter Store SecureString** (`/rebar/prod/opcert-ed25519-key`, AWS-managed KMS at rest). The
server is **stateless and store-read-only**: it returns the envelope in the response and the
**client** persists the self-authenticating DSSE envelope as a `SIGNATURE` event on the tickets
branch — so the server never writes the tickets branch.

**Rejected alternatives** (round-5 cost-verified against the account's real AWS bill):

- **Lambda** — rejected: the 15-minute hard ceiling and API Gateway's ~30s sync-integration timeout
  are incompatible with 30s–minutes gate runs.
- **A dedicated Fargate service** (+ NLB/VPC-link + Secrets Manager + a customer KMS CMK) — rejected
  on the **zero-fixed-monthly-cost constraint** (~60% increase on the account's whole monthly bill)
  buying **L4 isolation that ADR 0046 does not require** — the box is already the single controlled
  trusted environment holding Gerrit-admin and `LLM-Review` credentials, so box compromise already
  defeats the merge gate independently of the op-cert key.
- **A customer KMS CMK** (+$1/mo) and **AWS KMS-managed signing** — rejected (KMS asymmetric has no
  Ed25519, and the SSM SecureString mechanism is free and already in use).

### 10. HMAC removal via expand → contract

The legacy HMAC scheme is removed once op-certs verify asymmetrically. This is an **expand → contract**
migration (write-new/read-both, then drop HMAC), **not** a flag-day break, documented in the named
migration artifact **`docs/migrations/hmac-opcert-removal.md`** (cutover window + re-issue procedure —
re-run the gates; validity is computed on read). Authorship already signs asymmetrically (ADR 0045),
so plan-review and completion-verifier op-certs were the last HMAC consumers.

## Consequences

- Operation certificates become **third-party-verifiable, environment-attributed controls**: a
  high-assurance project can require a verdict it can trust was not forged by the developer who wanted
  to pass, while low-security projects keep the fast local-report behavior. No shared secret is ever
  needed to verify.
- The **authoritative era anchor** (storage-position, not self-chosen `merged_log_commit`) closes the
  backdating gap a revoked/not-yet-added key could otherwise exploit, while preserving overlapping
  rotation windows and a clean compromise kill-switch.
- Signer identity being **deployment config** (never repo-config) means the repo under verification
  cannot choose who signs it; the consumer/client policy keys stay separate and opt-in.
- Reusing the identity epic's merge-gate/ledger/era-rule keeps the two kinds independent in trust
  while avoiding a parallel copy of the plumbing.
- **This ADR is op-cert-scoped and defers to ADR 0046** for the accepted limitations it shares: **L1**
  anti-rollback rests on the append-only access control, not a standalone monotonic counter (ticket
  `2422`); **L2** no independent transparency log (idea `4e1d`); **L4** single controlled environment,
  no threshold/multi-sig (idea `5100`). Each remains a tracked, bounded, revisitable choice at the
  FISMA-Moderate target.

## Prior art / grounding

- **DSSE / SSHSIG** — PAE over exact bytes; `keyid` is an unauthenticated hint (verify against the
  pinned key, not the claimed one); OpenSSH Ed25519 signing.
- **in-toto / SLSA** — subject/state binding; non-falsifiable provenance produced by a trusted
  control plane; "provenance proves *how*, not that the inputs were good."
- **gittuf / RFC 8725** — log-position key validity and per-kind pinned scheme (never trust in-band
  algorithm selection); the storage-anchor era rule is the anti-rollback-aware form of this.
- **ADR 0046** — the security-posture ADR this document defers to for L1/L2/L4.
