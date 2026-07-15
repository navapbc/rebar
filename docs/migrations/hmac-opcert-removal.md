# Migration: removing the legacy HMAC scheme for op-cert kinds

This documents the two-phase **expand → contract** migration that moves the two OP-CERT
attestation kinds — `plan-review` (the claim gate) and `completion-verifier` (the close
gate) — off the legacy symmetric **HMAC-SHA256** scheme and onto asymmetric
`rebar.opcert.v1` op-certs (Ed25519 over a DSSE-PAE envelope, via the SSHSIG scheme).

The motivation is security: an HMAC attestation is signed with a **symmetric** secret
(`REBAR_SIGNING_KEY` / the gitignored `<tracker>/.signing-key`). That secret is shared by
every clone that can verify, so *anyone able to verify could also forge* a "certified"
verdict. An op-cert is signed with an environment's **private** key and verified against
its out-of-band-pinned **public** key, so verification confers no forging power.

## Expand

Shipped by the producer-signing seam story (8d8e) plus the op-cert storage / merge-gate
keystones — the **write-new / read-both** phase:

- **Write new.** `signing.sign_manifest` (and `rebar sign` / the MCP `sign_manifest`) mint
  a `rebar.opcert.v1` DSSE op-cert with the environment's auto-generated Ed25519 key
  (`<tracker>/.opcert-key`). The persisted `SIGNATURE` record carries an `envelope` (and
  `principal` / `material_fingerprint` / `merged_log_commit`) and **no** HMAC `signature`.
- **Read both.** The read path (`signing.verify_attestation_record`) dispatched on record
  **shape**: an `envelope`-bearing record verified through the op-cert verifier, while a
  legacy `signature`-bearing (HMAC) record still verified through the unchanged
  `verify_record` HMAC path. Old HMAC attestations and new envelope attestations coexisted
  on one store, kind-keyed.
- **Additive storage.** The op-cert record fields are present-only extensions; a legacy
  HMAC record is byte-unchanged, so a pre-upgrade clone preserves-and-ignores the new
  envelope events (it has no asymmetric verifier and reads them as UNSIGNED).

## Contract

Shipped by this story (8f1d) — the legacy HMAC scheme is **removed** for the op-cert
kinds, closing the dual-shape window:

- **No HMAC signing.** `sign_manifest` is envelope-only for the op-cert kinds (it was
  already so after the expand phase; there is no reachable HMAC fallback for these kinds).
- **No HMAC scheme registration.** The `HMAC-SHA256` scheme and the `plan-review` /
  `completion-verifier` → HMAC policy pins are gone from the attest registry
  (`src/rebar/attest/registry.py` `POLICY` + the deleted `hmac_legacy` module). Those two
  kinds now resolve **only** the asymmetric `sshsig` op-cert scheme.
- **No HMAC acceptance (validity-on-read).** A pre-existing HMAC-signed `plan-review` /
  `completion-verifier` record now reads **NOT-certified** (verdict `unknown_scheme`). The
  record is **not mutated** — the append-only event history is untouched — the verdict is
  simply recomputed on read. See ADR 0009 (reopen-invalidation / validity-on-read).

### Upgrade order

Follow the store rollout rule (as with the SSHSIG-authorship rollout and `TAG_DELTA`):
**upgrade the reconcile / reconciler / verify hosts first.** Older clones
preserve-and-ignore the envelope-bearing `SIGNATURE` events (they read them as unsigned
because they lack the asymmetric verifier), and `fsck` WARNs when the store holds event
types / schemes newer than the running binary. Upgrading the hosts that run the gates
first ensures the op-cert verdicts are read correctly everywhere before older readers are
retired.

### Re-issue procedure for still-live HMAC attestations

There is **no flag-day break** and no need to touch closed history. When a still-open
ticket's HMAC `plan-review` / `completion-verifier` attestation goes stale (reads
NOT-certified after the contract phase), simply **re-run the gate** to re-issue an
asymmetric op-cert:

- `plan-review`: re-run `rebar review-plan <id>` — a passing review re-signs the current
  plan as an op-cert, which the claim gate then consumes.
- `completion-verifier`: re-close the ticket (`transition <id> in_progress closed`) so the
  completion verifier re-runs and re-signs a PASS as an op-cert.

Closed tickets whose HMAC attestation is now stale are fine: their history is unchanged and
they can be re-closed to re-earn a signed op-cert if certification is needed again.

### Rollback posture

The required-environment policy is **opt-in and defaults off**. With no
`.rebar/trusted_environments.yaml` present and `verify.require_environment` unset, the
process keeps the low-security, process-record behavior (an op-cert signed by the local
environment certifies locally without a pinned trust root). This means the contract change
does not force any deployment to adopt trusted-environment pinning; that remains a separate,
opt-in hardening step. Turning the policy off again is a safe rollback of that hardening.

## `rebar.authorship.v1` is unaffected

The `rebar.authorship.v1` attestation kind (the identity epic) is **NOT** an op-cert kind
and is **NOT** touched by this migration. It was already asymmetric (SSHSIG over commit
authorship), it uses a different key, trust-root, and namespace, and it never used the
legacy HMAC scheme. Likewise the generic HMAC utility (`signing.compute_signature`,
`signing.verify_record`, the `.signing-key` genesis) remains available for any non-op-cert
consumer — only the two op-cert kinds are contracted.
