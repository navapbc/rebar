# Runbook — MCP per-client bearer PATs (provisioning + operator-driven rotation)

Scope: the per-client bearer PATs the box's `rebar-mcp` `static` verifier authenticates
(epic `jira-reb-3527` "Enable MCP on AWS"; ADR 0104 §1 — nginx `/mcp/` TLS edge + `static`
verifier + per-client PATs). Each client (Copilot, Codex, Claude) presents its own bearer PAT
as an `Authorization: Bearer <PAT>` header; the server holds only SHA-256 digests (ADR 0050 §4).

These PATs are treated like every other box secret: **SSM-sourced, materialized on-box,
gitignored, never committed**.

## How the credential material is delivered

Three SSM SecureString parameters hold the raw PATs (declared in `infra/terraform/ssm.tf`,
placeholder `CHANGEME`, `lifecycle.ignore_changes = [value]` so an operator-seeded value is never
reverted):

- `/rebar/prod/mcp-client-pat-copilot`
- `/rebar/prod/mcp-client-pat-codex`
- `/rebar/prod/mcp-client-pat-claude`

On every boot / deploy, `infra/scripts/fetch-secrets.sh` materializes them into **two on-box
sinks** — the box `.env` is `rsync`-EXCLUDED (gotcha `f600`), so the values are materialized on
the box, not baked into the rsync'd source tree:

1. The **raw value** lands in the 0600 `infra/compose/.env` as `MCP_CLIENT_PAT_COPILOT`,
   `MCP_CLIENT_PAT_CODEX`, `MCP_CLIENT_PAT_CLAUDE`.
2. The **tokens file the verifier reads** — `infra/compose/mcp-static-tokens.json` (0600), wired
   to `mcp.auth_static_tokens_file` — carries one record per **populated** client that references
   the env var by **name** via `token_env` (never a plaintext `token`, never the raw value in the
   tokens file). A blank slot's record is **omitted** so the verifier never sees an empty
   `token_env`; the file is **always** created (bug `beb1` — a missing bind-mount source would
   make docker create a directory). When **all** slots are blank the file is `{"tokens": []}` and
   the `static` verifier **fails-closed at startup** ("defines no tokens") until an operator
   populates at least one PAT.

Both materialized files are gitignored and `rsync`-excluded, so no secret is ever committed and a
re-materialize is not clobbered by `rsync --delete`.

## Provisioning (first time)

1. Generate a bearer PAT for each client (a long, high-entropy random token).
2. Seed each SSM SecureString out-of-band (approval-gated; do **not** commit the value):

   ```sh
   aws ssm put-parameter --overwrite --type SecureString \
     --name /rebar/prod/mcp-client-pat-copilot --value "<copilot-pat>"
   # …repeat for -codex and -claude
   ```

3. Re-materialize on the box and restart `rebar-mcp` (see rotation below) so the verifier reads
   the new token set.
4. Wire each client locally: copy `mcp-clients.local.example.json` (repo-root, committed
   placeholder) to `mcp-clients.local.json` (gitignored) and fill in the real PATs + box host.

## Rotation (operator-driven)

Rotation is **operator-driven**. A pure value rotation (re-seeding an SSM SecureString) is **not**
a git change, so it does **not** advance `main`; the autodeploy timer (ADR 0079) tracks `main` and
therefore **no-ops on a value-only rotation**. The operator must drive it:

1. Re-seed the SSM SecureString(s) with the new value(s) (`aws ssm put-parameter --overwrite …`).
2. **Re-materialize** on the box: re-run `infra/scripts/fetch-secrets.sh` (rewrites `.env` +
   `mcp-static-tokens.json`).
3. **Restart / replace the `rebar-mcp` process/container** (the ADR 0104 blue-green swap).

Step 3 is **mandatory**: the `static` verifier loads its token set **once at `__init__`**
(`StaticBearerVerifier.__init__` → `_load_static_tokens`; there is **no per-request re-read**), so
"reload" here means **reconstructing the verifier** — restarting/replacing the process — not merely
rewriting the tokens file. An operator who only re-materializes the file but does not restart
`rebar-mcp` sees **no effect**: the old digest set stays live (the rotated token is rejected and
the retired token is still accepted) until the process is replaced.

## Back-out

Remove the three SSM params + the `fetch-secrets.sh` materialization; no existing secret path
changes. With the tokens file absent/empty the `static` verifier fails-closed at startup, so MCP
access is denied rather than left open.
