# ADR 0125 — Op-cert trust: validity-only by default, per-environment trust lists

**Status:** Accepted
**Date:** 2026-11-19
**Ticket:** `calcite-cursed-lovebird` / `02a7-a3ed-f72e-42b1`
**Epic:** `nonblack-brigandish-guillemot` / `0dd0-cecb-3932-4c90`
**Supersedes:** the repository key pinning of ADR 0049 (`.rebar/trusted_environments.yaml`, its
log-position key eras, and `rebar trusted-env`), and the part of ADR 0104 decision 3 that relies
on that pin file.

## Context

ADR 0049 made operation certificates (the plan-review and completion-verifier verdicts)
asymmetric and environment-attributed. To say which environments a project trusts, it pinned
environment public keys in a repository file, `.rebar/trusted_environments.yaml`, with
tickets-log positions marking when each key became valid and when it was revoked. ADR 0104
decision 3 then built the MCP server's enforcement plan on that file: the box signs under the
environment pinned there.

Verification today picks a trust root in this order (bug c21f): the verifying environment's own
op-cert key when the certificate names that environment (`own_key`), otherwise a key pinned for
the certificate's environment in the file (`pinned_environment`), otherwise the key embedded in
the signature itself (`envelope_key`), which proves the signature is self-consistent but not who
made it. Under `verify.require_environment` the embedded-key fallback is withheld.

Pinning keys in the repository has 3 problems:

- **It hardcodes one deployment's trust into every checkout.** Every clone, CI runner, review
  bot and developer machine inherits the same key list, whether or not it has any reason to
  trust those keys. Trust is a property of the verifier, not of the code.
- **It mixes signing with authorization.** Signing refused a key that did not match the pinned
  key for its claimed environment, so whether an environment could sign at all depended on a
  repository allow-list.
- **It makes ordinary rotation a code change** with era bookkeeping. A rotated-out key that was
  never marked revoked kept verifying, and a pre-rotation certificate needed a dedicated retry
  path (bug `luny-minute-nandine`).

The operator decided on 2026-11-19 that op-cert keys must not be hardcoded in the repository.

## Decision

**1. Principle: identity, authorization and op-cert certification are separate.** Anyone can
sign a plan review, code review or completion verification. Signing is never gated on an
allowed identity or key. Whether a signer is who it claims to be is a separate question, which
a verifier may check against its own trust configuration.

**2. Default verification is validity-only.** An op-cert verifies when:

- its SSHSIG signature verifies under the public key embedded in the signature, for the
  certificate's principal and the `rebar.opcert.v1` namespace; and
- its signed subject binds the ticket, the attestation kind and the material, so it cannot be
  replayed onto another ticket, slot or state.

The result reports which key signed (`signing_key`, the key's `SHA256:` fingerprint as
`ssh-keygen -l` prints it) and which environment the certificate names (`key_id`, the
principal), with trust basis `envelope_key`. It does not judge whether that key is trusted.
After this ADR the trust bases are `envelope_key` and `trust_list` (below); `own_key` and
`pinned_environment` are removed, because the embedded key already verifies a certificate made
under any of the environment's own keys, current or rotated.

**3. Optional per-environment trust list.** An environment that wants to restrict which signers
it accepts sets the config key `verify.trusted_opcert_keys` (environment override
`REBAR_VERIFY_TRUSTED_OPCERT_KEYS`). Each entry is

```
<env_id> <key-type> <base64> [comment]
```

that is, an environment id followed by an OpenSSH public key, the same shape as an SSH
`allowed_signers` line. The list lives in the verifying environment's own configuration — its
user config (`~/.config/rebar/config.toml`), its environment variables, its deployment, or a CI
job's protected variables — and the repository's own `rebar.toml` does not set it. When the
list is set:

- a valid, correctly bound certificate whose (principal, key) pair is listed passes, with trust
  basis `trust_list`;
- a valid, correctly bound certificate whose pair is not listed fails with verdict `untrusted`,
  naming the key fingerprint and the environment;
- an invalid or wrongly bound certificate still fails as `mismatch` or `invalid` first;
- a list that cannot be read or parsed fails closed: the configuration load raises, and no
  certificate verifies as trusted.

Keys in the list carry no eras. Removing a key from the list untrusts every certificate it
signed; keeping a rotated-out key listed keeps its earlier certificates trusted. So rotation
adds the new key and keeps the retired one listed while its certificates still matter; a
compromise removes the key, after which the affected tickets are re-certified (or, in an
enforcing merge gate, `verify.opcert_enforce_since` is advanced past them).

**4. `verify.require_environment` requires a trust list.** It keeps its meaning — the
certificate's principal must equal the named environment — but a principal is only a claim the
certificate makes about itself. So with `require_environment` set and no trust list, verification
refuses with `foreign_key` and says a trust list is required. The merge gate `rebar verify-opcert`
verifies completion certificates against the gating environment's trust list the same way, so
turning on enforcement in a merge gate means setting `require_environment`,
`opcert_enforce_since` and `trusted_opcert_keys` together in that gate's environment, which the
change under test cannot edit.

**5. The repository pin file and its tooling are removed.** `.rebar/trusted_environments.yaml`,
its loader, its log-position key eras, and the `rebar trusted-env` command are deleted. No code
reads a repository-pinned op-cert trust file, and signing never consults any trust list.

**6. Rebar's own MCP server trusts only its own signatures.** Its deployment
(`infra/compose/docker-compose.yml` `mcp` service and the `mcp_run_new` container launch in
`infra/scripts/autodeploy.sh`) sets `REBAR_VERIFY_TRUSTED_OPCERT_KEYS` to the 3 keys that were
pinned: environment `9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65` with `nava-opcert-prod` and
`nava-opcert-prod rotated-20260907`, and environment `ade0f2ef-51cc-45a2-bb97-c09846cd3df5` with
`rebar-opcert`. The deployment list lands before the code stops reading the pin file, so the live
server never runs without a list. Its `/health` `opcert` field compares the bound signer against
that list.

## Consequences

- Developer clones, CI runners and the review bot have no list, so they verify validity only: a
  correctly signed and bound certificate from any environment passes, and the result shows which
  key and environment produced it.
- On the MCP server, a certificate signed in another environment (for example a developer's
  local `review-plan`) reads as `untrusted`, so claims and closes through the server need
  certificates the server's listed keys signed. This is the intended "trusts only its own
  signatures" posture.
- The verdict vocabulary gains `untrusted`; `trust_basis` becomes `envelope_key` or
  `trust_list` (the `own_key` and `pinned_environment` bases are gone); results gain
  `signing_key`.
- This repository's CI sets no list and no required environment, so its `verify-opcert` step
  stays advisory, as before. `infra/runbooks/mcp-opcert-enforcement-flip.md` describes enabling
  enforcement with all three settings.
- Key rotation is a configuration change in each verifying environment, not a code change.

## Alternatives rejected

- **Keep the repository pin file.** Rejected by the operator: keys must not be hardcoded in the
  repository, and one deployment's trust should not ride along with every checkout.
- **Per-key eras in the trust list.** Eras existed to tie a key's validity to tickets-log
  positions. A verifier that wants to stop trusting a key removes it from its own list, which is
  simpler and needs no position bookkeeping.
- **Gate signing on a pinned key.** Rejected by the separation principle: anyone may sign;
  trust is decided by whoever verifies.
