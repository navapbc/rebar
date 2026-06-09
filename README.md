# rebar

An event-sourced ticket system with a Jira reconciler, exposed three ways:

- **CLI** — the `rebar` command
- **Python library** — `import rebar`
- **MCP server** — `rebar-mcp` (stdio)

Tickets are stored as an append-only event log on a dedicated `tickets` git
orphan branch (worktree at `.tickets-tracker/`); state is computed by replaying
events. A level-triggered reconciler bidirectionally syncs tickets with Jira.

This project was extracted from the `digital-service-orchestra` Claude Code
plugin. The bash + Python engine is wrapped verbatim under `src/rebar/_engine/`;
the three interfaces are thin layers over it.

## Requirements

- Python ≥ 3.10
- System binaries: `git`, `jq`, `flock`, `bash`
- `acli` (Atlassian CLI) — only for live Jira reconciliation

## Install

Published to PyPI as **`nava-rebar`** (the import package and commands stay
`rebar` / `rebar-mcp`):

```bash
pipx install nava-rebar              # isolated CLI: rebar
pip install 'nava-rebar[mcp]'        # + MCP server: rebar-mcp
pip install nava-rebar               # library: import rebar

brew install navapbc/rebar/rebar     # Homebrew tap (CLI)
```

From a source checkout:

```bash
pip install .            # library + CLI
pip install '.[mcp]'     # + MCP server (FastMCP)
```

The engine (bash dispatcher + `ticket-*.sh` + python helpers) is exec'd as real
files, so rebar must be installed **unpacked to a real on-disk directory**:
zipimport / zip-safe installs are unsupported. Standard wheel installs (hatchling
builds unpacked) and editable installs satisfy this; `engine_dir()` asserts it at
the first engine call and fails loudly otherwise.

## CLI

```bash
rebar init                                   # create the tickets branch + worktree
rebar create story "Add login page"          # prints the ticket id
rebar list [--status=open] [--has-tag=...]   # JSON array
rebar show <id|alias>                         # compiled ticket state (JSON)
rebar transition <id> <current> <target>      # optimistic-concurrency status change
rebar comment <id> "<body>"
rebar link <id1> <id2> blocks|depends_on|relates_to
rebar deps <id>                               # dependency graph
rebar ready                                   # tickets with all blockers closed
rebar next-batch <epic-id>                    # unblocked tickets under an epic's hierarchy
rebar reconcile [--mode dry-run|reconcile-check|live]   # Jira sync (default: dry-run)
```

Repo root is resolved from `REBAR_ROOT` (or `PROJECT_ROOT`), falling back to the
git toplevel of the working directory.

## Python library

```python
import rebar

rebar.init_repo(repo_root="/path/to/repo")
tid = rebar.create_ticket("story", "Add login page", priority=2)
ticket = rebar.show_ticket(tid)                 # dict
tickets = rebar.list_tickets(status="open")     # list[dict]
try:
    rebar.transition(tid, "open", "in_progress")
except rebar.ConcurrencyError:
    ...                                          # ticket changed since last read

result = rebar.reconcile("dry-run")              # Jira sync (non-mutating)

# Native, in-process reads (no subprocess):
from rebar import reduce_all_tickets, reduce_ticket
```

## MCP server

```bash
rebar-mcp          # stdio transport
```

Exposes ticket operations as MCP tools. `reconcile` defaults to `dry-run`
(`live` requires `REBAR_MCP_ALLOW_RECONCILE_LIVE=1`). Set `REBAR_MCP_READONLY=1`
to expose only the read tools (no write/mutation tools).

Register it in an MCP client (e.g. Claude Desktop/Code) — zero-preinstall via
`uvx`:

```json
{
  "mcpServers": {
    "rebar": {
      "command": "uvx",
      "args": ["--from", "nava-rebar[mcp]", "rebar-mcp"],
      "env": { "REBAR_ROOT": "/path/to/your/repo" }
    }
  }
}
```

A registry manifest for the [MCP Registry](https://github.com/modelcontextprotocol/registry)
lives in [`server.json`](server.json) (`io.github.navapbc/rebar`); publish it with
the `mcp-publisher` CLI (`mcp-publisher login github` → `mcp-publisher publish`).
The registry verifies PyPI-package ownership via this annotation:

mcp-name: io.github.navapbc/rebar

## License

Apache-2.0 — see [`LICENSE`](LICENSE).

## Configuration

Optional `.rebar/config.conf` (or `.rebar.conf`) at the repo root, flat
`key=value`:

```ini
ticket.display_mode=auto          # auto | canonical | alias | short
ticket_clarity.threshold=70
verify.require_verdict_for_close=true
```

## Tests

```bash
pip install '.[dev]'                          # includes the mcp extra for interface tests
pytest                                        # full Python suite
pytest tests/interfaces                       # interface-parity tier only
bash tests/scripts/test-ticket-create.sh      # bash engine tests
```

The Python suite is sub-divided by concern:

- `tests/scripts`, `tests/unit` — the engine (reducer, graph, reconciler) and bash scripts.
- `tests/interfaces` — proves the **library, CLI, and MCP** interfaces behave
  identically over one git-backed store:
  - `test_parity.py` runs each operation through all three interfaces (and a
    cross-interface coherence check: write via one, read via the others);
  - `test_surface.py` pins the per-interface capability surface (e.g. MCP has no
    `init`; there is no `classify`);
  - `test_library.py` / `test_cli.py` / `test_mcp.py` cover per-interface
    specifics (typed exceptions, exit-code passthrough, read-only/live gates).
