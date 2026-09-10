# Connecting a client to a remote rebar MCP endpoint (static bearer PAT)

This guide wires the three supported MCP clients — **GitHub Copilot CLI**, **Codex**, and
**Claude Code** — to a rebar MCP server that is deployed remotely over HTTP behind a TLS edge
(epic `jira-reb-3527` "Enable MCP on AWS"; ADR 0104 / deft-evolutive-mosasaur). Each client
presents a per-client **bearer PAT** in its HTTP authorization header, which the server's
[`static` verifier](mcp-auth.md#2-static-bearer-token-verifier) authenticates. None of the three
requires OAuth for this deployment — one static-bearer shape covers all three.

Copy-ready example configs live under [`examples/mcp-clients/`](../examples/mcp-clients/).

## Prerequisites — get your PAT (never commit it)

The per-client PATs are provisioned and rotated by the operator; see
[`infra/runbooks/mcp-client-pats.md`](../infra/runbooks/mcp-client-pats.md) for the full model.
On a developer machine you obtain your PATs the same way the runbook's step 4 describes: copy the
committed placeholder [`mcp-clients.local.example.json`](../mcp-clients.local.example.json) to the
**gitignored** `mcp-clients.local.json` and fill in the real per-client PATs plus the box host.
`mcp-clients.local.json` is listed in `.gitignore` and must **never** be committed.

Export the project-scope PAT variables into the environment before launching the clients:

```sh
export MCP_CLIENT_PAT_CLI="…"     # shared by Claude Code + GitHub Copilot CLI (clients.cli)
export MCP_CLIENT_PAT_CODEX="…"   # from mcp-clients.local.json → clients.codex
```

`MCP_CLIENT_PAT_CLI` is a local alias for the existing claude PAT value; the server-side SSM
slots and static-token records stay unchanged. Every client config below references env vars
**by name** — no PAT literal is ever written to a config file.

### Make the export DURABLE — a bare `export` is not setup

The `export` lines above set the variable in **one shell process only**. It is persisted
nowhere: it dies when that shell exits, and a client launched from any *other* shell — a new
terminal tab, an editor's integrated terminal, a login shell started tomorrow — sees the variable
unset. The client then authenticates with nothing, the server returns `401`, and the client
**silently omits `rebar` from its tool list**. Nothing in the client says "your PAT was missing";
the server simply is not there. Treat a one-off `export` as a *test*, never as setup.

Pick one durable delivery mechanism and use it for both project variables:

- **Shell rc file (simplest).** Append the `export` lines to the rc file that runs for the shells
  you actually launch clients from — `~/.zshrc` on a default macOS zsh, `~/.bashrc`/`~/.bash_profile`
  on bash. Open a **new** shell afterwards and confirm with `printenv MCP_CLIENT_PAT_CODEX` or
  `printenv MCP_CLIENT_PAT_CLI` (which prints the value — do this only on a screen you are willing
  to expose). The file now contains the PAT in cleartext, so `chmod 600` it and never place it in a
  repo or a dotfiles repository.
- **macOS GUI launch environment.** GUI-launched clients do not necessarily source shell startup
  files. Publish the same names with `launchctl setenv` from your local refresh helper before
  launching Codex/Copilot/Claude from an app launcher.
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
  for that client. For this repository's project-scoped remote MCP entry, Copilot and Claude share
  `MCP_CLIENT_PAT_CLI`; Codex keeps `MCP_CLIENT_PAT_CODEX`. This fires even when the misnamed
  variable *is* set, because the operator who exports the canonical name and the config that reads
  a different one never meet.

Fixing only one of the two can leave the server omitted, so `doctor` reports them independently.
Findings name **variables only** — no credential value is ever read into the report.

The endpoint is the external TLS URL, e.g. `https://rebar.solutions.navateam.com/mcp/` (substitute
your box host). The server binds loopback behind the nginx `/mcp/` TLS edge; see
[mcp-auth.md §5](mcp-auth.md#5-behind-a-proxy-tls-at-the-edge).

## Copilot CLI

Project config file: the repository-root [`.mcp.json`](../.mcp.json). Copilot CLI 1.0.83 loads
workspace `.mcp.json` before `.github/mcp.json` and lets workspace entries override user-level
entries, so the rebar server is scoped to this checkout instead of every directory on the host.
The CLI expands the header env var from the environment, so the PAT stays out of the file. For a
user-level fallback/template, see
[`examples/mcp-clients/copilot/mcp-config.json`](../examples/mcp-clients/copilot/mcp-config.json):

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

Verify from this repository: `copilot mcp get rebar` (or `copilot mcp list`) lists the `rebar` server as repository/workspace-scoped and its URL. From a non-project directory, `rebar` should not appear once the old user-level entry is removed.

## Codex

Config file: `~/.codex/config.toml` (or a trusted project's `.codex/config.toml`). Codex reads a
bearer token from the environment via `bearer_token_env_var` and sends it in the HTTP
authorization header, so the PAT stays out of the file. Merge the entry from
[`examples/mcp-clients/codex/config.toml`](../examples/mcp-clients/codex/config.toml):

```toml
[mcp_servers.rebar]
url = "https://rebar.solutions.navateam.com/mcp/"
bearer_token_env_var = "MCP_CLIENT_PAT_CODEX"
startup_timeout_sec = 120
```

Verify from this repository: `codex mcp get rebar`. From a non-project directory, it should fail or show no `rebar` entry once the old user-level block is removed.

## Claude Code

Project config file: the repository-root [`.mcp.json`](../.mcp.json) (or `~/.claude.json` for a
user-level fallback). Claude Code expands `${VAR}` in a `headers` value from the environment.
The project config uses `MCP_CLIENT_PAT_CLI`, the same local alias Copilot uses, so one machine
slot covers both CLI clients. A user-level template is in
[`examples/mcp-clients/claude/.mcp.json`](../examples/mcp-clients/claude/.mcp.json):

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

Verify from this repository: `claude mcp list` lists the project `rebar` server. After the global cutover, the user-level `~/.claude.json` `mcpServers` map should no longer contain `rebar`.

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
