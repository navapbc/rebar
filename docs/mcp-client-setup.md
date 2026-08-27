# Connecting a client to a remote rebar MCP endpoint (static bearer PAT)

This guide wires the three supported MCP clients — **GitHub Copilot CLI**, **Codex**, and
**Claude Code** — to a rebar MCP server that is deployed remotely over HTTP behind a TLS edge
(epic `jira-reb-3527` "Enable MCP on AWS"; ADR 0104 / deft-evolutive-mosasaur). Each client
presents a per-client **bearer PAT** in an `Authorization: Bearer <PAT>` header, which the
server's [`static` verifier](mcp-auth.md#2-static-bearer-token-verifier) authenticates. None of
the three requires OAuth for this deployment — one static-bearer shape covers all three.

Copy-ready example configs live under [`examples/mcp-clients/`](../examples/mcp-clients/).

## Prerequisites — get your PAT (never commit it)

The per-client PATs are provisioned and rotated by the operator; see
[`infra/runbooks/mcp-client-pats.md`](../infra/runbooks/mcp-client-pats.md) for the full model.
On a developer machine you obtain your PATs the same way the runbook's step 4 describes: copy the
committed placeholder [`mcp-clients.local.example.json`](../mcp-clients.local.example.json) to the
**gitignored** `mcp-clients.local.json` and fill in the real per-client PATs plus the box host.
`mcp-clients.local.json` is listed in `.gitignore` and must **never** be committed.

Export each PAT into the environment before launching its client. The env var **names** match the
box-side names in the runbook, so the shape is uniform:

```sh
export MCP_CLIENT_PAT_COPILOT="…"   # from mcp-clients.local.json → clients.copilot
export MCP_CLIENT_PAT_CODEX="…"     # from mcp-clients.local.json → clients.codex
export MCP_CLIENT_PAT_CLAUDE="…"    # from mcp-clients.local.json → clients.claude
```

Every client config below references these env vars **by name** — no PAT literal is ever written
to a config file.

### Make the export DURABLE — a bare `export` is not setup

The `export` lines above set the variable in **one shell process only**. It is persisted
nowhere: it dies when that shell exits, and a client launched from any *other* shell — a new
terminal tab, an editor's integrated terminal, a login shell started tomorrow — sees the variable
unset. The client then authenticates with nothing, the server returns `401`, and the client
**silently omits `rebar` from its tool list**. Nothing in the client says "your PAT was missing";
the server simply is not there. Treat a one-off `export` as a *test*, never as setup.

Pick one durable delivery mechanism and use it for all three variables:

- **Shell rc file (simplest).** Append the `export` lines to the rc file that runs for the shells
  you actually launch clients from — `~/.zshrc` on a default macOS zsh, `~/.bashrc`/`~/.bash_profile`
  on bash. Open a **new** shell afterwards and confirm with `printenv MCP_CLIENT_PAT_CODEX` (which
  prints the value — do this only on a screen you are willing to expose). The file now contains the
  PAT in cleartext, so `chmod 600` it and never place it in a repo or a dotfiles repository.
- **Your secret manager (preferred where you have one).** Keep the PAT in the manager and have the
  rc file *fetch* it, so no cleartext secret lands on disk — e.g.
  `export MCP_CLIENT_PAT_CODEX="$(op read op://Private/rebar-codex-pat/credential)"` (1Password CLI),
  or the equivalent `pass`/`gopass`/`security find-generic-password` call. Rotation then happens in
  one place.

Whichever you choose: **never commit the value**, and never paste it into a client config file, a
ticket, a commit message, or a chat transcript. The configs below deliberately reference the
variable **by name** so that the secret has exactly one home.

### Check the wiring with `rebar doctor`

`rebar doctor` reads each client's config and reports two faults that both end in the same
symptom (`rebar` missing from the tool list):

- **`pat-unresolvable`** — the config names a bearer env var that is unset or empty in the current
  environment. This is what a transient `export` looks like after the shell that held it exits.
- **`stale-pat-env-name`** — the config names a bearer env var that is **not** the canonical name
  for that client (the canonical names are exactly the three above). This fires even when the
  misnamed variable *is* set, because the operator who exports the canonical name and the config
  that reads a different one never meet.

Fixing only one of the two can leave the server omitted, so `doctor` reports them independently.
Findings name **variables only** — no credential value is ever read into the report.

The endpoint is the external TLS URL, e.g. `https://rebar.solutions.navateam.com/mcp/` (substitute
your box host). The server binds loopback behind the nginx `/mcp/` TLS edge; see
[mcp-auth.md §5](mcp-auth.md#5-behind-a-proxy-tls-at-the-edge).

## Copilot CLI

Config file: `~/.copilot/mcp-config.json`. The CLI expands `$VAR` in a `headers` value from the
environment, so the PAT stays out of the file. Copy
[`examples/mcp-clients/copilot/mcp-config.json`](../examples/mcp-clients/copilot/mcp-config.json)
or add the entry with the CLI:

```sh
copilot mcp add --transport http rebar https://rebar.solutions.navateam.com/mcp/ \
  --header "Authorization: Bearer $MCP_CLIENT_PAT_COPILOT"
```

```jsonc
{
  "mcpServers": {
    "rebar": {
      "type": "http",
      "url": "https://rebar.solutions.navateam.com/mcp/",
      "headers": { "Authorization": "Bearer $MCP_CLIENT_PAT_COPILOT" },
      "tools": ["*"]
    }
  }
}
```

Verify: `copilot mcp get rebar` (lists the `rebar` server and its URL).

## Codex

Config file: `~/.codex/config.toml` (or a trusted project's `.codex/config.toml`). Codex reads a
bearer token from the environment via `bearer_token_env_var` and sends it as `Authorization: Bearer
<PAT>`, so the PAT stays out of the file. Merge the entry from
[`examples/mcp-clients/codex/config.toml`](../examples/mcp-clients/codex/config.toml):

```toml
[mcp_servers.rebar]
url = "https://rebar.solutions.navateam.com/mcp/"
bearer_token_env_var = "MCP_CLIENT_PAT_CODEX"
```

Verify: `codex mcp list` (or `codex mcp get rebar`).

## Claude Code

Config file: a project `.mcp.json` (or `~/.claude.json`). Claude Code expands `${VAR}` in a
`headers` value from the environment. Copy
[`examples/mcp-clients/claude/.mcp.json`](../examples/mcp-clients/claude/.mcp.json) or add the
server with the CLI:

```sh
claude mcp add --transport http rebar https://rebar.solutions.navateam.com/mcp/ \
  --header "Authorization: Bearer ${MCP_CLIENT_PAT_CLAUDE}"
```

```jsonc
{
  "mcpServers": {
    "rebar": {
      "type": "http",
      "url": "https://rebar.solutions.navateam.com/mcp/",
      "headers": { "Authorization": "Bearer ${MCP_CLIENT_PAT_CLAUDE}" }
    }
  }
}
```

Verify: `claude mcp list` (lists the `rebar` server).

> **Static-header gotcha (all clients, most visible in Claude Code).** A static `Authorization`
> header takes **precedence** and does **not** fall back to OAuth if the server rejects it. A
> wrong or expired PAT therefore fails **hard** with a `401 Unauthorized` surfaced to the client —
> there is no silent OAuth retry. If you see a 401, re-export the correct PAT (rotation may have
> invalidated the old one — see the runbook) rather than expecting an interactive login.

## Troubleshooting

- **`401 Unauthorized`** — the bearer PAT is missing, wrong, or expired. Confirm the env var is
  exported in the shell that launched the client, and that the value matches the current
  `mcp-clients.local.json`. After an operator rotation the old PAT is rejected until you re-copy.
- **Server not listed** — check the config file location for your client (above) and that the
  entry carries `"type": "http"` (Copilot/Claude) or lives under `[mcp_servers.rebar]` (Codex).
- **Connection refused / TLS error** — verify the box host and that the `/mcp/` TLS edge is
  reachable; this is the endpoint concern owned by the `esok` work, not the client config.

## Rollback

Remove the three server entries (or the copied example files). No server-side change is involved;
the client configs are purely additive.
