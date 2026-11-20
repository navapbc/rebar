# Runbook — flip on enforceable MCP-minted op-certs

Turn the `verify-opcert` merge-gate from **advisory** to **enforcing** so that every
post-deploy ticket close carries a valid `completion-verifier` operation certificate signed by
the on-box `rebar-mcp` server's trusted signing environment. This is a **deliberately deferred,
human-run operator step** — the code that provisions the container to sign under the box
environment (story `spoiled-bionic-goose`) lands separately and is live *before* you perform
this flip.

Decision basis: ADR [`0049`](../../docs/adr/0049-opcert-asymmetric.md) (operation certificates:
asymmetric, environment-attributed, optionally required) and ADR
[`0104`](../../docs/adr/0104-mcp-on-box.md) decision 3 (enforceable MCP-minted op-certs, Option
A — provision signing first, flip the gate on safely via expand-contract).

> **Why a human step, not part of the change.** Setting `verify.require_environment` in the
> authoritative `rebar.toml` makes the `verify-identity` / `verify-opcert` merge-gate enforce a
> completion-verifier cert for **every** post-boundary close **repo-wide**. Until closes
> actually route through the box's `9f1c8e42-…` signer, a normal **local-CLI close** signs
> under the *local* environment (or genesis key) and would **fail** the gate. So the flip must
> only happen once the precondition below holds, and an operator — not the automated
> change — owns that judgement.

---

## Preconditions (ALL must hold before you flip)

1. **The signing provisioning is deployed and live.** The `rebar-mcp` compose service is
   running with:
   - `REBAR_OPCERT_ENV_ID = 9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65` (the box env_id — the string
     `nava-opcert-prod` is only that key's comment/label, not the env_id), and
   - `REBAR_OPCERT_KEY_PATH = /run/secrets/opcert-ed25519-key`, the SSM-materialized
     Ed25519 op-cert key bind-mounted read-only (materialized by `infra/scripts/fetch-secrets.sh`,
     shared with the `opcert` gate service).

   `REBAR_IDENTITY_SIGNING_KEY` is a different key: it names the rebar bot signing key
   (`/run/secrets/rebar-bot-signing-key`) that signs the server's tracker writes, and it plays
   no part in op-cert signing. With `REBAR_OPCERT_ENV_ID` set, the server refuses to start if
   either key is missing or invalid. That includes a blank `/rebar/prod/rebar-bot-signing-key`
   slot: `fetch-secrets.sh` then writes an empty key file, which review-bot tolerates (it writes
   unsigned events) but the MCP server does not.

   The server composes its startup op-cert signer from these
   (`rebar.mcp_server.compose_startup_opcert_binding`) and binds it around serving, so the
   certified-op tools mint certs whose principal is `9f1c8e42-…`.

2. **The MCP server trusts its own keys.** Per ADR 0125 the repository pins no op-cert keys.
   The MCP deployment sets its own trust list, `REBAR_VERIFY_TRUSTED_OPCERT_KEYS`, identically in
   `infra/compose/docker-compose.yml` (the `mcp` service) and in `mcp_run_new` in
   `infra/scripts/autodeploy.sh`. Each comma-separated entry is
   `<env_id> <key-type> <base64> [comment]`. Confirm the live server's `/health` `opcert` field
   reads `expected: true`, `matched: true`, `trust_source: "trust_list"` and no `error`
   (read-only, through SSM: `curl -s http://127.0.0.1:<live port>/health` on the box).

3. **The gating environment has a trust list.** `verify.require_environment` refuses every
   certificate unless the verifying environment also sets `verify.trusted_opcert_keys`. For this
   repository the gate is the `verify-opcert` step of `.github/workflows/verify-identity.yml`, so
   the list goes into that job's environment as `REBAR_VERIFY_TRUSTED_OPCERT_KEYS`, from the CI
   provider's protected variables — never committed to `rebar.toml`. List the same keys the MCP
   deployment lists. Confirm a freshly minted cert verifies in a **dry run** with that list in the
   environment:
   ```bash
   REBAR_VERIFY_TRUSTED_OPCERT_KEYS='<same value as the MCP deployment>' \
     rebar verify-opcert --require-environment 9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65 \
     --since <candidate-boundary>
   ```
   It must report recently box-closed tickets as satisfied (exit 0), not "missing op-cert" or
   "untrusted".

4. **The box is the close path for post-boundary work.** Every close that will fall *after* the
   boundary must go through the on-box MCP server, **not** a local-CLI close: a local close signs
   under the local environment, whose key is not on the gate's list, and would fail the gate.

---

## Flip on (the authoritative change)

Set the pair in the authoritative `rebar.toml`, and the trust list in the gate's environment
(precondition 3):

```toml
[verify]
# Which trusted environment must have signed the completion-verifier op-cert.
require_environment = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"
# Deploy-time grandfather boundary: the tickets-branch log tip (a git ref/SHA on the tickets
# branch) captured at flip time. Only closes ANCHORED as a descendant of this ref are enforced;
# everything closed at or before it is grandfathered (advisory).
opcert_enforce_since = "<tickets-branch tip SHA at flip time>"
```

- **Capture `opcert_enforce_since` at flip time.** Read the current tickets-branch tip and use
  that SHA:
  ```bash
  git -C .tickets-tracker rev-parse HEAD   # the rebar tracker worktree on the checkout
  ```
  This is the flip's **grandfathering**: historical closes across the repo are **not**
  retroactively failed (which, without the boundary, would break the merge gate on every
  pre-existing close).
- **The flip takes effect after it merges, not in its own gate run.** `rebar verify-opcert`
  reads its `[verify]` settings from the `rebar.toml` committed on `origin/main`, never from the change under test, so the flip change itself is still gated
  by main's previous, advisory settings.
- **Set both keys in the same change, after the trust list exists.** `require_environment`
  without the boundary would enforce every historical close; the boundary without
  `require_environment` enforces nothing; `require_environment` without a trust list refuses
  everything (and `rebar verify-opcert` exits 2). Together they enforce **only** post-deploy
  closes.
- `require_environment` gates the **completion-verifier** merge lane (`rebar verify-opcert`).
  It ALSO restricts the local claim/close verification path (`verify_signature` / the
  plan-review claim gate) in any environment that reads this `rebar.toml`: there only
  certificates naming that environment, signed by a key that environment's
  `verify.trusted_opcert_keys` lists, are accepted. An environment with no list refuses them
  all. Flipping the pair therefore tightens plan-review consumption as well as the close lane —
  check that whichever environment agents run `review-plan` and `claim` in has the list.

After the change merges, confirm on the authoritative config (with the gate's list in the
environment):

```bash
rebar verify-opcert    # posture now comes from rebar.toml (no CLI flags)
```

A post-boundary close lacking a valid, listed `9f1c8e42-…` cert now **fails** (exit 1);
pre-boundary closes remain advisory (exit 0).

---

## Rollback (config revert)

Rollback is a pure config revert — no data migration and no key changes:

1. **Unset `verify.require_environment`** in `rebar.toml` (leave or drop
   `opcert_enforce_since`; with no required environment it has no effect). Land it through
   Gerrit.
2. `verify-opcert` returns to **advisory everywhere** (exit 0). Leaving the gate's
   `verify.trusted_opcert_keys` in place is harmless; remove it to return that environment to
   validity-only verification.

The flip is fully additive and reversible: no new environment or key is created, and no
historical close is retroactively failed. Re-enabling `require_environment` later restores
enforcement without any re-signing, as long as the signing keys are still listed.

---

## Related

- ADR [`0125`](../../docs/adr/0125-opcert-trust-per-environment.md) — validity-only op-cert
  verification by default; per-environment trust lists replace the repository pin file.
- `infra/scripts/fetch-secrets.sh` materializes the SSM parameter
  `/rebar/prod/opcert-ed25519-key` (the source) INTO the container bind-mount path
  `/run/secrets/opcert-ed25519-key` (the destination that `REBAR_OPCERT_KEY_PATH` points
  at), shared by the `opcert` and `mcp` services. The SSM parameter name and the bind-mount path
  are the same key at two hops, not two different keys.
- `infra/compose/docker-compose.yml` — the `mcp` service's `REBAR_OPCERT_ENV_ID`,
  `REBAR_OPCERT_KEY_PATH`, op-cert key bind-mount and `REBAR_VERIFY_TRUSTED_OPCERT_KEYS` (and,
  separately, the `REBAR_IDENTITY_SIGNING_KEY` bot-key bind-mount for authorship);
  `infra/scripts/autodeploy.sh` `mcp_run_new` mirrors them.
- `infra/runbooks/two-vote-gate-rollback.md` — the Gerrit submit-gate posture this gate feeds.
