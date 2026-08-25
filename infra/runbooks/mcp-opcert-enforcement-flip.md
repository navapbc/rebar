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
   - `REBAR_IDENTITY_SIGNING_KEY = /run/secrets/opcert-ed25519-key`, the SSM-materialized
     Ed25519 key bind-mounted read-only (materialized by `infra/scripts/fetch-secrets.sh`,
     shared with the `opcert` gate service).

   The server composes its startup op-cert signer from these
   (`rebar.mcp_server.compose_startup_opcert_binding`) and binds it around serving, so the
   certified-op tools mint certs whose principal is `9f1c8e42-…`. Confirm a freshly minted cert
   verifies against the pinned public key with a **dry run**:
   ```bash
   rebar verify-opcert --require-environment 9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65 \
     --since <candidate-boundary>
   ```
   It must report the recently box-closed tickets as satisfied (exit 0), not "missing op-cert".

2. **The box is the close path for post-boundary work.** Every close that will fall *after* the
   boundary must go through the on-box MCP server (which signs under `9f1c8e42-…`), **not** a
   local-CLI close (which signs under the local env and would fail the gate). This is the
   expand-contract ordering: **move-1 signing must be live before the flip.**

3. **The environment is pinned.** `.rebar/trusted_environments.yaml` on `main` already carries
   the `9f1c8e42-…` env with its real `ssh-ed25519` public key and an `added_at_log_position`
   era anchor. This flip adds **no** new pin and **no** key material — verify the entry is
   present and unchanged.

---

## Flip on (the authoritative change)

Set the two `[verify]` keys **together** in the authoritative `rebar.toml`, in one change,
reviewed through Gerrit:

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
  git -C .tickets-tracker rev-parse HEAD   # the rebar tracker worktree on this checkout
  ```
  This makes the flip **grandfathering**: historical closes across the repo are **not**
  retroactively failed (which, without the boundary, would break the `verify-identity`
  merge-gate for every pre-existing close).
- **Set both keys in the same change.** `require_environment` without a boundary would enforce
  every historical close; a boundary without `require_environment` enforces nothing. Together
  they enforce **only** post-deploy closes.
- `require_environment` gates the **completion-verifier** lane (the close gate). Plan-review
  certs are minted under the same environment (trusted where the claim/plan-review gate consumes
  them) but are **not** the lane `require_environment` enforces.

After the change merges, confirm on the authoritative config:
```bash
rebar verify-opcert    # posture now comes from rebar.toml (no CLI flags)
```
A post-boundary close lacking a valid `9f1c8e42-…` cert now **fails** (exit 1); pre-boundary
closes remain advisory (exit 0).

---

## Rollback (config revert)

Rollback is a pure config revert — no data migration, no key changes:

1. **Unset `verify.require_environment`** in `rebar.toml` (leave or drop `opcert_enforce_since`;
   with no required environment it has no effect). Land it through Gerrit.
2. `verify-opcert` returns to **advisory everywhere** (exit 0). The existing opcert /
   remote-cert trust path is unchanged — a validly signed pinned-env cert still verifies — so
   re-enabling `require_environment` later restores enforcement without any re-signing.

This is fully additive and reversible: no new environment or key was created, and no historical
close was retroactively failed.

---

## Related

- `infra/scripts/fetch-secrets.sh` — materializes the SSM parameter
  `/rebar/prod/opcert-ed25519-key` (the source) INTO the container bind-mount path
  `/run/secrets/opcert-ed25519-key` (the destination that `REBAR_IDENTITY_SIGNING_KEY` points
  at), shared by the `opcert` and `mcp` services. The SSM parameter name and the bind-mount path
  are the same key at two hops, not two different keys.
- `infra/compose/docker-compose.yml` — the `mcp` service's `REBAR_OPCERT_ENV_ID` +
  `REBAR_IDENTITY_SIGNING_KEY` + key bind-mount.
- `infra/runbooks/two-vote-gate-rollback.md` — the Gerrit submit-gate posture this gate feeds.
