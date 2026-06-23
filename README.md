# rebar

A git-native ticket system for coordinating coding agents — and the humans
working alongside them.

Point several agents at one repo and they immediately need a shared place to
coordinate: to claim work without grabbing the same ticket, record what they
discover, and hand off cleanly — while your teammates stay in the loop through
Jira. rebar makes the tracker *part of the repo itself*, so it travels with every
clone, needs no database or daemon, and lets many agents and sessions write at
once without merge conflicts or lost work.

It's an event-sourced ticket system with a Jira reconciler, exposed three ways:

- **CLI** — the `rebar` command
- **Python library** — `import rebar`
- **MCP server** — `rebar-mcp` (stdio)

Tickets are stored as an append-only event log on a dedicated `tickets` git
orphan branch (worktree at `.tickets-tracker/`); state is computed by replaying
events. A level-triggered reconciler bidirectionally syncs tickets with Jira.

This project was extracted from the `digital-service-orchestra` Claude Code
plugin. It began as a bash + Python engine; that engine has since been fully ported
to in-process Python (see `docs/bash-migration.md`). The reconciler ships under
`src/rebar/_engine/` as package data, and the three interfaces are thin layers over
the in-process core.

## Why rebar

If you run coding agents against a repo, you eventually want to run *several* at
once — and the moment you do, they need a shared place to coordinate. Most
trackers weren't built for that:

- **They're heavy.** A daemon to babysit or a local database to keep running,
  with dependencies thick enough that a routine upgrade can break your work
  tracking across machines.
- **They don't travel with the code.** State lives outside the repo, so a fresh
  clone doesn't come with its tickets.
- **They fight your git history.** A tracker that writes to your working branch
  tangles ticket churn into your source-code commits.
- **They have no concurrency story.** Nothing stops two agents from claiming the
  same work or clobbering each other's state, and concurrent edits produce merge
  conflicts you resolve by hand — or lose.
- **They buckle at scale.** Speed and usability fall off past a few hundred
  tickets.

**rebar's answer is to make the tracker part of the repo.** Tickets are an
append-only event log on a dedicated `tickets` orphan branch (linked in through a
gitignored worktree); current state is a fast, deterministic replay of that log.
That single decision pays off across the board:

- **Zero infrastructure, fully portable.** No database, no daemon — just git and a
  lightweight Python install. Clone the repo and the tracker comes with it.
- **No commit interference.** Ticket events live on their own branch and never
  touch your source history. Every write auto-commits and auto-pushes, so activity
  is shared in real time.
- **Concurrency by design.** Each event gets a globally-unique filename, so
  parallel writes merge as a clean union, and the rare conflicting fork resolves
  deterministically — every clone converges with no lost data. `claim` is an
  atomic, optimistic-concurrency primitive: agents grab work without stepping on
  each other.
- **Built to scale.** The event log plus cached replay stays fast as tickets grow.

On top of that foundation, rebar adds what parallel agent work actually needs:

- **Bidirectional Jira sync** — agents work in rebar, teammates work in Jira, and
  a level-triggered reconciler keeps the two in step.
- **Conflict-aware scheduling** — tickets record their file impact, so
  `next-batch` hands parallel agents work that won't collide on the same files.
- **Scratch space** — an invisible per-ticket channel for subagents to pass notes
  to one another.
- **Quality gates** — clarity, acceptance-criteria, dispatch-readiness, and
  repo-wide health checks keep work dispatch-ready.
- **Provenance links** — `discovered_from` ties emergent work back to the ticket
  that surfaced it.
- **One store, three interfaces** — drive it from the CLI, a Python library, or
  the MCP server.

## Requirements

**Runtime (system):**
- Python ≥ 3.11
- `git` — required (the store is a git orphan branch + worktree). The engine is
  pure in-process Python; `bash` and `jq` are **not** required at runtime.
- `flock` from **util-linux** — recommended for robust write serialization, but
  **not strictly required**: it is not on `PATH` by default on macOS
  (`brew install util-linux`), and when no util-linux `flock` is found rebar falls
  back to a `mkdir`-based lock automatically. (A non-util-linux `flock` such as
  BusyBox's is ignored in favor of the fallback.)
- `acli` (Atlassian CLI) — only for **live** Jira reconciliation.

**Python dependencies — runtime (prod) vs. rebar development.** rebar keeps a
deliberately tiny footprint; the distinction is between *running* rebar (and its
optional capabilities) and *developing rebar itself*:

- **Runtime — base.** `pip install nava-rebar` gives the `rebar` CLI + `import
  rebar` library + the **lean workflow engine** (author/validate/render/run
  *scripted* workflows). Its ONLY runtime dependency is **`pyyaml`** (the workflow
  DSL loader, epic a88f) — the engine core and reconciler are otherwise
  stdlib-only.
- **Runtime — optional capability extras** (install only what you use; each is
  lazy-imported, so the base stays light and CI enforces that):
  - **`[mcp]`** — the `rebar-mcp` server (`mcp>=1.2`).
  - **`[agents]`** — the LLM agent-operations framework + **agentic workflow
    steps** (`rebar review`, the `code_review` workflow): the provider-agnostic
    pydantic-ai runtime (`pydantic-ai-slim[anthropic]` + `json-repair`).
  - **`[eval]`** — prompt evaluation (`rebar prompt eval`): Inspect AI. An
    authoring/CI capability, not needed to serve.
  - **`[tracing]`** — the OTLP trace sink (write-only; OpenTelemetry is never read
    back into a rebar decision).
- **Development (working ON rebar).** `pip install -e '.[dev]'` adds the
  test/lint/type tooling (`pytest`, `ruff`, `mypy`, `jsonschema`, `hatchling`) and
  self-references `[agents]` so the validation tests **run** rather than skip. It
  is **required to run the full test suite** (the interface-parity tests import the
  MCP server, so they error — not skip — without `mcp`).
  - **Node/npm** are needed **only** for the workflow visual editor's front-end —
    *rebuilding* its vendored bundle (`src/rebar/llm/workflow/editor_assets/`, the
    bpmn-js editor) and running the faithful editor **E2E tier** (`tests/e2e/`, which
    drives the real bpmn-io libraries). Both are developer-only: the built bundle is
    committed/shipped, and the E2E tier self-skips when Node is absent, so neither the
    base install nor the default test suite needs Node. See
    [docs/workflow-editor.md](docs/workflow-editor.md).

See [Install](#install) and [Tests](#tests).

## Install

rebar ships from one Python package — PyPI distribution **`nava-rebar`** (the
import package and commands stay `rebar` / `rebar-mcp`). Pick the channel that
fits. (System prerequisites in all cases: `git` and `python3` (≥ 3.11); a
util-linux `flock` is used for write serialization when present, with a `mkdir`
fallback otherwise; `acli` only for live Jira reconciliation.)

### Homebrew (CLI)

```bash
brew install navapbc/rebar/rebar
# or: brew tap navapbc/rebar && brew install rebar
```

Installs the `rebar` CLI (and the `rebar` library inside the formula's venv). For
the MCP server via Homebrew users, install the `[mcp]` extra with pipx/uvx below.

### PyPI — pipx / pip

**Runtime (prod) — install what you'll run:**

```bash
pipx install nava-rebar              # isolated CLI on PATH: rebar (+ lean workflow engine)
pip  install nava-rebar              # library: import rebar  (only runtime dep: pyyaml)
pip  install 'nava-rebar[mcp]'       # + MCP server: rebar-mcp
pip  install 'nava-rebar[agents]'    # + LLM agent ops + agentic workflow steps (rebar.llm)
pip  install 'nava-rebar[eval]'      # + prompt evaluation: `rebar prompt eval` (Inspect AI)
pip  install 'nava-rebar[tracing]'   # + OTLP trace sink (write-only)
pip  install 'nava-rebar[agents,eval,tracing]'   # the union, if you want it all
```

The base install runs **scripted** workflows (`rebar workflow new/validate/show/run`)
with no extra; **agentic** workflow steps and `rebar review` need `[agents]`. Authoring
a workflow **visually** — `rebar workflow edit <file>`, a local bpmn-js editor that
round-trips the diagram back to the IR — also needs no extra and no Node/npm: the editor
front-end ships pre-built in the wheel and is served locally (no CDN). See
[docs/workflow-editor.md](docs/workflow-editor.md).

The `[agents]` extra adds the optional **LLM agent-operations framework**
(`rebar.llm`) — tool-using agents that review tickets/code and emit structured
findings, over library / CLI (`rebar review`) / MCP. It is multi-provider
(**Claude** and **ChatGPT** out of the box, plus Gemini and OpenAI-compatible local
servers like LMStudio/Ollama via `REBAR_LLM_MODEL`/`REBAR_LLM_MODEL_PROVIDER`/
`REBAR_LLM_BASE_URL`) and is **never required by core rebar** — none of the LLM
stack is installed or imported unless you opt into this extra (CI enforces it);
see [docs/llm-framework.md](docs/llm-framework.md).

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
`reconcile` is dry-run unless `REBAR_MCP_ALLOW_JIRA_SYNC=1` (deprecated alias
`REBAR_MCP_ALLOW_RECONCILE_LIVE`). Both flags
accept any case-insensitive truthy value — `1`, `true`, or `yes` (surrounding
whitespace tolerated); anything else (incl. unset) is off.

### From source

```bash
git clone https://github.com/navapbc/rebar && cd rebar
pip install .              # library + CLI (runtime; pyyaml only)
pip install '.[mcp]'      # + MCP server (FastMCP)
# Developing rebar itself — the full dev environment (test/lint/type tooling +
# the agents stack so the LLM validation tests RUN, not skip):
pip install -e '.[dev]'
```

> **Packaging note — why rebar installs *unpacked* to disk.** The library, CLI,
> MCP server, and the whole read/write core run **in-process** in Python. The one
> component that runs as a subprocess is the Jira **reconciler**, which ships under
> `src/rebar/_engine/` as package **data** (`python -m rebar_reconciler`, plus the
> `jira-capability-probe.py` script and the alias wordlist): it is launched and
> read from the filesystem as real on-disk files, so the package must be installed
> unpacked to a real directory and **zipimport / zip-safe bundles (zipapp, shiv,
> PEX, Lambda zips) are unsupported**. Every standard install satisfies this:
> pip/pipx wheels (hatchling builds unpacked), editable installs, and Homebrew all
> land real files. `engine_dir()` asserts the engine dir is present on disk at the
> first reconciler call and fails loudly otherwise.

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
rebar sign <id> '["ran tests: PASS", "lint clean"]'   # HMAC-sign a manifest of verified steps
rebar verify-signature <id>                   # certify the steps match the signature (exit 0=certified)
rebar reconcile [--mode dry-run|reconcile-check|live]   # Jira sync (default: dry-run)
```

Run `rebar help` (or `rebar --help` / `-h`) for the subcommand overview, and
`rebar <subcommand> --help` (or `rebar help <subcommand>`) for a specific
subcommand's usage — `--help` prints usage and never executes the command.
Help is only recognized as the first argument after the subcommand, so a
`--help`/`-h`/`help` that appears inside a free-text parameter (title, comment
body, search query, …) is treated as literal text, not a help request.

Repo root is resolved from `REBAR_ROOT`, falling back to the git toplevel of the
working directory.

**Structured output.** Every data-returning command emits machine-readable JSON
via the canonical `--output json` flag (short `-o json`; `--output llm` gives a
token-minified shape for `show`/`list`/`ready`). Each distinct JSON shape is
documented by a JSON Schema and validated across the CLI, library, and MCP in CI.
See [docs/output-schemas.md](docs/output-schemas.md) for the per-command contract
and the schema source-of-truth.

**`validate` vs. the per-ticket gates.** `rebar validate` takes **no ticket id** —
it scans the whole store and prints an overall tracker-health score (1-5, exit
0-4) bucketed into critical / major / minor / warning findings (`--output json`,
`--terse`, `--verbose`, `--fix`). Passing it a ticket id errors. The *per-ticket*
quality gates are separate commands that each take an `<id>`: `clarity-check`,
`check-ac`, `quality-check`. They are **structural floor checks** — they verify a
ticket is *shaped* like dispatchable work, not that the content is good. Every
type needs an `## Acceptance Criteria` checklist (`- [ ]` items); `check-ac` and
`clarity-check` both require it. See the per-type ticket template (Why/What/Scope
for stories, Reproduction Steps for bugs, Success Criteria/Context for epics) in
[CLAUDE.md](CLAUDE.md#ticket-template-the-gates-enforce).

**Links.** `rebar link <id1> <id2> <relation>` **requires** a relation; the six
relations are `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
`discovered_from`. `rebar unlink <source> <target>` takes **no** relation
argument — it is pair-scoped and removes the **most-recently-created** link
between that ordered pair, one per call, so to remove multiple links between the
same pair you call `unlink` repeatedly. Note that **blocking** links
(`blocks`/`depends_on`) may be promoted up the parent hierarchy when created (see
below), so `unlink` must target the **promoted (ancestor)** endpoint to remove
such a link.

### Signing a manifest of verified steps

`rebar sign <id> <manifest>` records a **cryptographic attestation** on a ticket:
a manifest (a JSON array of verified-step strings) plus an HMAC-SHA256 signature
computed with a key that is **specific to the environment** rebar runs in. The key
is resolved from `REBAR_SIGNING_KEY` (injected out-of-band into a shared
deployment — e.g. an MCP server) or, failing that, a per-environment
`.signing-key` file generated on first use (gitignored, never committed, never
shared). `rebar verify-signature <id>` recomputes the HMAC with the local key and
**certifies** that the recorded steps still match the signature:

```bash
rebar sign abcd-1234 '["unit tests: PASS", "security review: clean", "deployed to staging"]'
rebar verify-signature abcd-1234        # SIGNATURE: certified — verified steps match the signature
```

The signature binds both the ticket id and the manifest, so it cannot be replayed
onto another ticket and any edit to the step list invalidates it. Because the key
never leaves the environment, `verify-signature` reports `foreign_key` (rather
than `certified`) when a record was signed by a *different* environment — only the
environment that holds the key can certify its own attestations. The signature is
stored as a normal append-only `SIGNATURE` event, so it replays into `show`
output, survives compaction, and flows to other clones like any other write.

The signing key is a shared secret (HMAC), so the attestation proves a signature
was produced by a holder of the environment key and that the steps are unaltered
since — it is **not** a public-key identity. Anyone who can read the
`.signing-key` file (written `0600`, owner-only) or the injected `REBAR_SIGNING_KEY`
can forge a `certified` record, so protect read access to the environment
accordingly.

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

### Reads share one freshness policy across CLI, library, and MCP

Every **read** — `show`, `list`, `ready`, `search`, `deps` — first runs a
throttled (**≤1/min**), best-effort `git fetch` + reconverge of the local
`tickets` branch with `origin/tickets`, so a read reflects collaborators' pushes
within at most a minute. This is **one contract shared by all three interfaces**:
CLI, library (`rebar.list_tickets()`, …), and the MCP read tools all resolve
through a single read implementation. (Previously only CLI reads synced, leaving
MCP — the primary agent surface — with the *stalest* reads; that divergence is
gone.)

**Opt out** for a pure-local replay (offline work, tight loops, or right after a
write that already synced): set `REBAR_SYNC_PULL=off` (honored everywhere;
deprecated alias `REBAR_NO_SYNC=1`) or pass `--no-pull` to any read subcommand
(e.g. `rebar list --no-pull`; deprecated alias `--no-sync`). Only the network
fetch/merge is skipped; the local reduce/cache path is unchanged. See
[`docs/concurrency.md`](docs/concurrency.md#read-freshness-policy-uniform-across-cli-library-and-mcp).

### The on-disk store is not human-readable — read it with `rebar`

The `tickets` branch is rebar's **internal storage format, not a document for
people to read.** Each ticket is a directory of append-only JSON **event** files
(`${hlc}-${uuid}-${TYPE}.json`); the current state of a ticket is what you get by
**replaying** those events through the reducer. Two consequences follow:

- **It isn't laid out in order.** Event files are named by a Hybrid Logical Clock
  + UUID and merge across clones as a union, so the files for one ticket are not a
  top-to-bottom narrative — they are an unordered set that only becomes meaningful
  after the reducer sorts and folds them. A single `EDIT`/`STATUS`/`TAG_DELTA`
  file in isolation tells you a delta, not the ticket.
- **The current state is computed, never stored.** Nothing on the branch holds the
  compiled "current" ticket except a local, rebuildable `.cache.json` (gitignored).
  Reading the raw files by hand will mislead you — a later event may supersede an
  earlier one, a `SNAPSHOT` may fold many away, and concurrent forks resolve by a
  deterministic rule you'd have to apply yourself.

So **don't `cat` the `.tickets-tracker/` worktree to find out where a ticket
stands** — use the read commands, which run the reducer for you: `rebar show
<id>`, `rebar list`, `rebar deps <id>`, `rebar search <query>` (CLI), the matching
library calls (`rebar.show_ticket(...)`), or the MCP read tools.

For reference, [`docs/sample-ticket-log.jsonl`](docs/sample-ticket-log.jsonl) is a
small **synthetic** event log (one event per line) showing what the underlying
data actually looks like — a two-agent epic + child tickets exercising
create/claim/comment/link/tag/file-impact/sign/transition. Note that its lines are
deliberately **not** in timestamp order: that is the point. The event body schema
is documented in [`docs/event-schema.md`](docs/event-schema.md).

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

# Cryptographic attestation (environment-bound HMAC):
rebar.sign_manifest(tid, ["unit tests: PASS", "security review: clean"])
verdict = rebar.verify_signature(tid)            # {"verified": True, "verdict": "certified", ...}

# Native, in-process reads (no subprocess):
from rebar import reduce_all_tickets, reduce_ticket
```

## MCP server

```bash
rebar-mcp          # stdio transport
```

Exposes ticket operations as MCP tools. `reconcile` defaults to `dry-run`
(`live` requires `REBAR_MCP_ALLOW_JIRA_SYNC=1`, deprecated alias
`REBAR_MCP_ALLOW_RECONCILE_LIVE`). Set `REBAR_MCP_READONLY=1`
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

rebar reads **TOML** config from `[tool.rebar]` in `pyproject.toml` or a standalone
`rebar.toml` (nearest up-tree, stopping at `.git`), falling back to a user config at
`~/.config/rebar/config.toml` (honoring `$XDG_CONFIG_HOME`). Precedence, highest
first: **`rebar -c SECTION.KEY=VALUE` / CLI flag > `REBAR_<SECTION>_<KEY>` env >
project config > user config > built-in default.** `rebar config` prints the resolved
values and which layer each came from.

```toml
[tool.rebar]
verify.require_signature_for_close = true  # gate story/epic close on a certified
                                           # signature at HEAD (rebar sign); default
                                           # false. Alias: verify.require_verdict_for_close
ticket.display_mode = "auto"               # auto | canonical | alias | short
compact.threshold   = 10
sync.push = "always"                       # always | async | off
sync.pull = "on"                           # on | off
mcp.readonly = false
scratch.base_dir = ""                      # default <repo>/.rebar/scratch
```

The full key set, the `REBAR_<KEY>` env names, and deprecation aliases are in
[`docs/config.md`](docs/config.md). The legacy flat `.rebar/config.conf`
(`key=value`) is still read during a deprecation window for back-compat.

When the close gate is enabled, closing a story/epic requires a **certified
signature made at the current HEAD** — sign a manifest of verified steps
(`rebar sign <id> '[...]'`) then `rebar transition <id> closed`; re-sign if HEAD
moved, or bypass with `--force-close=<reason>`. This replaces the older
`--verdict-hash`/`compute-verdict-hash.sh` gate, which is now deprecated.

rebar keeps its writable state under `.rebar/` at the repo root. The `scratch`
store defaults to `<repo>/.rebar/scratch/` (override with `scratch.base_dir` /
`REBAR_SCRATCH_BASE_DIR`; the unprefixed `SCRATCH_BASE_DIR` is a deprecated alias),
and one-shot migration stamps are written under `.rebar/` as well.

## Tests

Run the suite from an environment with the `[dev]` extra installed (a venv is
recommended); the interface-parity tests import the MCP server, so a bare
interpreter without the `mcp` extra will **error** rather than skip.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'                       # editable + pytest, mcp, jsonschema
pytest -m "not integration"                   # the single entry point (CI runs this)
pytest tests/interfaces                       # interface-parity tier only
pytest tests/scripts                          # engine/reconciler tier only
```

**`pytest` is the single entry point.** The engine is pure in-process Python
(the bash engine and its `.sh` suites were removed in the bash→Python migration —
see `docs/bash-migration.md`). CI (`.github/workflows/test.yml`) runs
`pytest -m "not integration"` on
Ubuntu and macOS for every push and PR. The `integration` tier (live Jira /
network) is **excluded** from that default run; run it explicitly with credentials
via `pytest -m integration`.

The Python suite is sub-divided by concern:

- `tests/scripts`, `tests/unit` — the in-process engine (reducer, graph, reconciler).
- `tests/interfaces` — proves the **library, CLI, and MCP** interfaces behave
  identically over one git-backed store:
  - `test_parity.py` runs each operation through all three interfaces (and a
    cross-interface coherence check: write via one, read via the others);
  - `test_surface.py` pins the per-interface capability surface (e.g. MCP has no
    `init`; there is no `classify`);
  - `test_library.py` / `test_cli.py` / `test_mcp.py` cover per-interface
    specifics (typed exceptions, exit-code passthrough, read-only/live gates).
