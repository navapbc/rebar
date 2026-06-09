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

rebar ships from one Python package — PyPI distribution **`nava-rebar`** (the
import package and commands stay `rebar` / `rebar-mcp`). Pick the channel that
fits. (System prerequisites in all cases: `git`, `jq`, `flock`, `bash`,
`python3`; `acli` only for live Jira reconciliation.)

### Homebrew (CLI)

```bash
brew install navapbc/rebar/rebar
# or: brew tap navapbc/rebar && brew install rebar
```

Installs the `rebar` CLI (and the `rebar` library inside the formula's venv). For
the MCP server via Homebrew users, install the `[mcp]` extra with pipx/uvx below.

### PyPI — pipx / pip

```bash
pipx install nava-rebar              # isolated CLI on PATH: rebar
pip  install nava-rebar              # library: import rebar
pip  install 'nava-rebar[mcp]'       # + MCP server: rebar-mcp
```

### MCP server — from the MCP Registry

Listed in the [MCP Registry](https://registry.modelcontextprotocol.io) as
**`io.github.navapbc/rebar`**. Registry-aware MCP clients can add it by that
name; or register it directly in your client config (zero pre-install via
`uvx`):

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

(Already pip/pipx-installed `nava-rebar[mcp]`? Use `"command": "rebar-mcp"`
instead.) Server flags: `REBAR_MCP_READONLY=1` exposes only read tools;
`reconcile` is dry-run unless `REBAR_MCP_ALLOW_RECONCILE_LIVE=1`.

### From source

```bash
git clone https://github.com/navapbc/rebar && cd rebar
pip install .              # library + CLI
pip install '.[mcp]'      # + MCP server (FastMCP)
pip install -e '.[dev]'   # editable + test deps (pytest, mcp)
```

> **Packaging note:** the engine (bash dispatcher + `ticket-*.sh` + python
> helpers) is exec'd as real files, so rebar must be installed **unpacked to a
> real on-disk directory** — zipimport / zip-safe installs are unsupported.
> Standard wheel installs (hatchling builds unpacked) and editable installs
> satisfy this; `engine_dir()` asserts it at the first engine call and fails
> loudly otherwise.

## CLI

```bash
rebar init                                   # create the tickets branch + worktree
rebar create story "Add login page"          # prints the ticket id
rebar list [--status=open] [--has-tag=...]   # JSON array
rebar show <id|alias>                         # compiled ticket state (JSON)
rebar transition <id> <current> <target>      # optimistic-concurrency status change
rebar comment <id> "<body>"
rebar link <id1> <id2> <relation>            # relation REQUIRED (see relations below)
rebar unlink <source> <target>               # remove ONE link for the ordered pair (no relation arg)
rebar deps <id>                               # dependency graph
rebar ready                                   # tickets with all blockers closed
rebar next-batch <epic-id>                    # unblocked tickets under an epic's hierarchy
rebar validate                                # repo-wide tracker health (NO ticket id; whole-store score 1-5)
rebar clarity-check <id> / check-ac <id> / quality-check <id>   # per-ticket quality gates
rebar reconcile [--mode dry-run|reconcile-check|live]   # Jira sync (default: dry-run)
```

Repo root is resolved from `REBAR_ROOT` (or `PROJECT_ROOT`), falling back to the
git toplevel of the working directory.

**`validate` vs. the per-ticket gates.** `rebar validate` takes **no ticket id** —
it scans the whole store and prints an overall tracker-health score (1-5, exit
0-4) bucketed into critical / major / minor / warning findings (`--json`,
`--terse`, `--verbose`, `--fix`). Passing it a ticket id errors. The *per-ticket*
quality gates are separate commands that each take an `<id>`: `clarity-check`,
`check-ac`, `quality-check`.

**Links.** `rebar link <id1> <id2> <relation>` **requires** a relation; the six
relations are `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
`discovered_from`. `rebar unlink <source> <target>` takes **no** relation
argument — it is pair-scoped and removes the **most-recently-created** link
between that ordered pair, one per call, so to remove multiple links between the
same pair you call `unlink` repeatedly. Note that **blocking** links
(`blocks`/`depends_on`) may be promoted up the parent hierarchy when created (see
below), so `unlink` must target the **promoted (ancestor)** endpoint to remove
such a link.

### Hierarchy promotion of blocking links

For **blocking** dependencies only (`blocks`, `depends_on`), rebar promotes the
link endpoints up the parent hierarchy so the dependency sits between tickets at
a comparable level (epic↔epic, story↔story, task/bug↔task/bug). When it does so
it emits a `REDIRECT: A→B promoted to …` note. Non-blocking relations
(`relates_to`, `duplicates`, `supersedes`, `discovered_from`) are linked exactly
as given, with no promotion.

### The store auto-commits and auto-pushes every write

Every rebar **write** (`create`, `edit`, `transition`, `claim`, `link`, …)
auto-commits its event to the `tickets` branch **and** auto-pushes that branch to
`origin/tickets` whenever an `origin` remote exists. **Local ticket activity is
therefore shared with the remote immediately** — including test/scratch tickets,
so be deliberate when working against a repo with a shared `tickets` remote. The
push is **best-effort**: with no `origin` remote nothing is pushed, and a push
failure (e.g. non-fast-forward it cannot auto-merge, or no network) never fails
the write — it leaves the local commit intact and the branch diverged.
`rebar fsck` reports `PUSH_PENDING` when the local `tickets` branch is ahead of
`origin/tickets`, so unpushed activity is observable. See
[`docs/concurrency.md`](docs/concurrency.md) for the push/merge-retry algorithm.

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
to expose only the read tools (no write/mutation tools). To register it in an MCP
client (registry name `io.github.navapbc/rebar`, or a direct `uvx` config), see
[Install → MCP server](#mcp-server--from-the-mcp-registry) above.

**Maintainers:** the registry manifest lives in [`server.json`](server.json);
publish/update it with the `mcp-publisher` CLI (see `docs/releasing.md`). The
registry verifies PyPI-package ownership via this annotation (kept in this
README, which is the PyPI long description):

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
