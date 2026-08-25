# Example MCP client configs — remote rebar MCP endpoint (static bearer PAT)

Copy-ready configs that wire the three supported MCP clients to a **remote** rebar MCP server over
HTTP with a per-client **static bearer PAT** (epic `jira-reb-3527` "Enable MCP on AWS"; ADR 0104 /
deft-evolutive-mosasaur). Each client sends `Authorization: Bearer <PAT>`; the server's `static`
verifier authenticates it. None of the three needs OAuth for this deployment.

| Client | Copy this file to | PAT source (env var) |
|---|---|---|
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` | `$MCP_CLIENT_PAT_COPILOT` |
| Codex | `~/.codex/config.toml` (merge the `[mcp_servers.rebar]` table) | `bearer_token_env_var = "MCP_CLIENT_PAT_CODEX"` |
| Claude Code | project `.mcp.json` (or `~/.claude.json`) | `${MCP_CLIENT_PAT_CLAUDE}` |

**No secret is stored in these files.** Each references its PAT by environment-variable name only.
The real PATs come from the **gitignored** `mcp-clients.local.json` (copy the repo-root placeholder
[`mcp-clients.local.example.json`](../../mcp-clients.local.example.json) and fill it in). Export the
env vars before launching each client:

```sh
export MCP_CLIENT_PAT_COPILOT="…"
export MCP_CLIENT_PAT_CODEX="…"
export MCP_CLIENT_PAT_CLAUDE="…"
```

Substitute your own box host for `rebar.solutions.navateam.com` in each config's `url`.

Full walkthrough, verification commands, the static-header 401 gotcha, and troubleshooting:
[`docs/mcp-client-setup.md`](../../docs/mcp-client-setup.md). PAT provisioning and rotation:
[`infra/runbooks/mcp-client-pats.md`](../../infra/runbooks/mcp-client-pats.md).
