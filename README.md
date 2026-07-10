# rebar

[![PyPI version](https://img.shields.io/pypi/v/nava-rebar)](https://pypi.org/project/nava-rebar/)
[![Python versions](https://img.shields.io/pypi/pyversions/nava-rebar)](https://pypi.org/project/nava-rebar/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![CI](https://github.com/navapbc/rebar/actions/workflows/test.yml/badge.svg)](https://github.com/navapbc/rebar/actions/workflows/test.yml)

**An event-sourced, git-backed ticket store + Jira reconciler built for agent
swarms — one store, exposed as a Python library, a **CLI**, and an **MCP** server.**

![rebar's core loop: `rebar ready` → `rebar claim` → `rebar transition … closed`](docs/assets/rebar-demo.svg)

- **Three surfaces, one store** — drive rebar as a **CLI** (`rebar`), a Python
  library (`import rebar`), or an **MCP** server (`rebar-mcp`).
- **The tracker lives in the repo** — tickets are an append-only event log on a
  `tickets` git branch; no database, no daemon, and it travels with every clone.
- **Built for parallel agents** — atomic claims, convergent merges, and provenance
  links let many agents and sessions write at once without lost work.
- **Optional LLM gates** — review a ticket's *plan* before work, its *completion*
  before close, and its *code* before it merges.
- **Bidirectional Jira sync** — a level-triggered reconciler keeps tickets and Jira
  in step, so teammates stay in the loop.
- **Dogfooded through two independent gates** — every change to rebar's own `main`
  must pass an LLM code review **and** CI, on Gerrit, before it lands.

## Install

```bash
pipx install nava-rebar          # the `rebar` CLI (add [mcp] / [agents] for those extras)
brew install navapbc/rebar/rebar # or via Homebrew
```

## Quickstart

Run one ticket end-to-end across all three surfaces. `rebar --help` (and
`rebar <command> --help`) is the authoritative command reference.

```bash
rebar init && rebar create task "Add a login page"    # CLI: init + create
rebar ready && rebar claim reel-lot-tea --assignee alice
rebar transition reel-lot-tea in_progress closed       # -> UNBLOCKED: …
```
```python
import rebar                                            # Python library
tid = rebar.create_ticket("task", "Add a login page")
rebar.claim(tid, assignee="alice"); rebar.transition(tid, "in_progress", "closed")
```
```json
{ "mcpServers": { "rebar": { "command": "uvx", "args": ["--from", "nava-rebar[mcp]", "rebar-mcp"] } } }
```

That's the whole loop — **init → create → ready → claim → close** — shared through the
repo so many agents (and teammates via Jira) coordinate without stepping on each other.

## How it works

rebar stores tickets as an **append-only event log** on a dedicated `tickets` git
orphan branch (worktree at `.tickets-tracker/`); ticket state is computed by replaying
events, and every write auto-commits and pushes so the store is shared immediately. A
**level-triggered reconciler** bidirectionally syncs tickets with Jira. The branch name
and worktree dir are configurable (`tracker.branch` / `tracker.dir` — see
[Configuration](#configuration)). Reads stay sub-second into the thousands of tickets;
for measured numbers and git-growth expectations see
[`docs/scale-envelope.md`](docs/scale-envelope.md).

**Documentation** lives under [`docs/`](docs/README.md) — start with the
[docs index](docs/README.md) (grouped by audience: user / operator / contributor /
agent) or the day-to-day [user guide](docs/user-guide.md).

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
  a level-triggered reconciler keeps the two in step. To run it **automatically** in
  CI, see [docs/jira-sync-setup.md](docs/jira-sync-setup.md) (the GitHub Actions
  reconcile-bridge + heartbeat-canary setup).
- **Conflict-aware scheduling** — tickets record their file impact, so
  `next-batch` hands parallel agents work that won't collide on the same files.
- **Scratch space** — an invisible per-ticket channel for subagents to pass notes
  to one another.
- **Structural quality gates** — clarity, acceptance-criteria, dispatch-readiness,
  and repo-wide health checks keep work dispatch-ready.
- **LLM review gates** *(optional)* — review an agent's *plan* before work starts,
  its *completion* before the ticket closes, and its *code* before it merges.
  Plan-review and code-review share one four-pass kernel — a finder cites evidence, a
  *separate* verifier tests each claim with atomic yes/no questions, and a
  **deterministic** policy (never the model) decides what blocks — so a review coaches
  with grounded, cited findings rather than a black-box score. A passing plan or
  completion review leaves an HMAC-signed attestation: a machine-checkable signal of
  rigorous agentic development, not vibe-coding.
- **Provenance links** — `discovered_from` ties emergent work back to the ticket
  that surfaced it.
- **One store, three interfaces** — drive it from the CLI, a Python library, or
  the MCP server.

## Requirements

**System prerequisites:**
- [Python](https://www.python.org) ≥ 3.11
- [`git`](https://git-scm.com) — required (the store is a git orphan branch +
  worktree). The engine is pure in-process Python; `bash` and `jq` are **not**
  required at runtime.
- [`flock`](https://github.com/util-linux/util-linux) from **util-linux** —
  recommended for robust write serialization, but **not strictly required**: it is
  not on `PATH` by default on macOS (`brew install util-linux`), and when no
  util-linux `flock` is found rebar falls back to a `mkdir`-based lock
  automatically. (A non-util-linux `flock` such as BusyBox's is ignored in favor of
  the fallback.)
- [`acli`](https://developer.atlassian.com/cloud/acli/) (Atlassian CLI) — only for
  **live** Jira reconciliation.

**Python dependencies.** A base install (`pip install nava-rebar`) — the `rebar`
CLI, the `import rebar` library, and the lean workflow engine — pulls only two
runtime dependencies: [`pyyaml`](https://pyyaml.org) (the workflow DSL loader) and
[`jsonschema`](https://python-jsonschema.readthedocs.io) (the schema-registry +
workflow input/output-contract validator); the engine core and reconciler are
otherwise stdlib-only. Everything else is an optional extra, lazy-imported so the
base stays light (CI enforces that):

- **Optional runtime capabilities** — install what you serve:
  - **`[mcp]`** — the [`rebar-mcp` server](https://modelcontextprotocol.io)
    (`mcp>=1.2`).
  - **`[agents]`** — the LLM agent-operations framework + agentic workflow steps
    (`rebar review`, the `code_review` workflow): the provider-agnostic
    [pydantic-ai](https://ai.pydantic.dev) runtime (`pydantic-ai-slim[anthropic]`)
    plus [`json-repair`](https://github.com/mangiucugna/json_repair).
- **Development & authoring extras** — not needed to run or serve rebar:
  - **`[eval]`** — prompt evaluation (`rebar prompt eval`) with
    [Inspect AI](https://inspect.aisi.org.uk); an authoring/CI capability.
  - **`[tracing]`** — an [OpenTelemetry](https://opentelemetry.io) OTLP trace sink
    (write-only; never read back into a rebar decision), for diagnostics.
  - **`[dev]`** — the test/lint/type tooling ([pytest](https://docs.pytest.org),
    [ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org),
    [hatchling](https://hatch.pypa.io)). `pip install -e '.[dev]'` also
    self-references `[agents]` so the validation tests **run** rather than skip, and
    is **required to run the full test suite** (the interface-parity tests import the
    MCP server, so they error — not skip — without `mcp`).
  - **[Node/npm](https://nodejs.org)** — needed **only** for the workflow visual
    editor's front-end: *rebuilding* its vendored bundle
    (`src/rebar/llm/workflow/editor_assets/`, the bpmn-js editor) and running the
    faithful editor **E2E tier** (`tests/e2e/`, which drives the real bpmn-io
    libraries). Both are developer-only — the built bundle is committed/shipped and
    the E2E tier self-skips when Node is absent — so neither the base install nor the
    default test suite needs Node. See [docs/workflow-editor.md](docs/workflow-editor.md).

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
pip  install nava-rebar              # library: import rebar  (runtime deps: pyyaml, jsonschema)
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
front-end ships pre-built in the wheel and is served locally (no CDN). For what the
engine is *for* — when to author a workflow vs a bespoke op, the YAML DSL, the
three-pass review pattern, and the prompt-library + eval seam — see
[docs/workflow-engine.md](docs/workflow-engine.md); for visual editing specifically see
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
`reconcile` is dry-run unless `REBAR_MCP_ALLOW_JIRA_SYNC=1`. Both flags
accept any case-insensitive truthy value — `1`, `true`, or `yes` (surrounding
whitespace tolerated); anything else (incl. unset) is off.

#### Private-repo fetch credentials (code-reading gates)

The LLM code-reading gates (`review_plan`, `verify_completion`, `review_ticket`,
`review_code`, `scan_spec`) default to **attested** mode: they `git fetch` the verified
ref from `origin` and read an immutable snapshot at the pinned SHA — never the server's
mutable checkout. So a server pointed (`REBAR_ROOT`) at a **private** repository needs
**read credentials to fetch**: a git credential helper, a deploy key, or a token in the
server's clone. With no credentials, attested mode **fails closed** with a descriptive,
actionable error (it never hangs on a prompt — `GIT_TERMINAL_PROMPT=0`); `source=local`
(read the in-place checkout, never signed) is the back-out that needs no fetch. Full
semantics, the HMAC trust model, and the snapshot env knobs (`REBAR_GATE_TMPDIR`, the
disk-space watermark, the EFS/NFS `flock` caveat) are in
[docs/repo-snapshot-gates.md](docs/repo-snapshot-gates.md).

### From source

```bash
git clone https://github.com/navapbc/rebar && cd rebar
pip install .              # library + CLI (runtime deps: pyyaml, jsonschema)
pip install '.[mcp]'      # + MCP server (FastMCP)
# Developing rebar itself — the full dev environment (test/lint/type tooling +
# the agents stack so the LLM validation tests RUN, not skip):
pip install -e '.[dev]'
```

> **Contributing changes?** GitHub is a **read-only mirror** — `main` only advances via
> Gerrit's two-vote gate (`LLM-Review` + `Verified`/CI). New contributors: start with the
> friendly walkthrough [docs/your-first-change.md](docs/your-first-change.md); the full
> reference is [CONTRIBUTING.md](CONTRIBUTING.md) (clone from Gerrit, push to
> `refs/for/main`, submit once both votes pass).

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

> **Advanced (optional) — gate commits with self-hosted code review.** Not needed
> for standard rebar use. If you want *every* commit to `main` automatically
> LLM-reviewed before it can land, you can self-host Gerrit + the rebar review-bot on
> AWS (the bot imports the same `rebar.llm` review kernel the MCP server exposes) and
> demote GitHub to a read-only mirror that only advances via Gerrit after the
> `LLM-Review` vote passes. See [docs/gerrit-aws-setup.md](docs/gerrit-aws-setup.md) for
> the server setup. *(This repo runs exactly that setup — see the contributor note above
> and [CONTRIBUTING.md](CONTRIBUTING.md).)*

## CLI

```bash
rebar init                                   # create the tickets branch + worktree
rebar create story "Add login page"          # prints the ticket id
rebar idea "Maybe cache the graph"           # capture an undesigned idea (born in status `idea`; unclaimable until promoted)
rebar show <id|alias>                         # compiled ticket state (JSON)
rebar summary <id> [<id> ...]                 # one-line summary + blocking status for one or more ids
rebar list [--status=open] [--has-tag=...]   # JSON array
rebar edit <id> [--title ... --priority ... --parent ... --add-tag ...]   # edit ticket fields / tags
rebar claim <id> --assignee <you>             # atomic open -> in_progress + assignee (the work-start primitive)
rebar transition <id> <current> <target>      # optimistic-concurrency status change
rebar reopen <id>                             # closed -> open (exit 10 if not currently closed)
rebar comment <id> "<body>"
rebar tag <id> <tag> / untag <id> <tag>       # add / remove a tag (convergent add/remove deltas)
rebar link <id1> <id2> <relation>            # relation REQUIRED (see relations below)
rebar unlink <source> <target>               # remove ONE link for the ordered pair (no relation arg)
rebar deps <id>                               # dependency graph
rebar search <query>                          # full-text over titles/descriptions/comments/tags (JSON)
rebar ready                                   # tickets with all blockers closed
rebar next-batch <epic-id>                    # unblocked tickets under an epic's hierarchy
rebar scratch <set|get|clear> <id> ...        # per-ticket scratch channel for subagents
rebar session-log "<entry>"                   # append to the current session_log (auto-rotates per session)
rebar session-logs [--limit=<n>]              # list the newest session_log tickets, newest first
rebar validate                                # repo-wide tracker health (NO ticket id; whole-store score 1-5)
rebar review-plan <id>                        # plan-review gate: DET floor + 3-pass advisory; signs an attestation (exit 0=PASS,1=BLOCK,2=INDETERMINATE)
rebar explain <criterion-id>                  # read the guide/rubric for a review criterion (pure registry read, no LLM call)
rebar export [-o FILE]                        # store -> NDJSON (one ticket/line; for jq/DuckDB/pandas + rebar->rebar migration)
rebar import [FILE]                           # import export NDJSON (fresh local ids; [--dry-run])
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

**Repo-wide health with `validate`.** `rebar validate` takes **no ticket id** — it
scans the whole store and prints an overall tracker-health score (1-5, exit 0-4)
bucketed into critical / major / minor / warning findings (`--output json`,
`--terse`, `--verbose`, `--fix`). Passing it a ticket id errors. (rebar also has
*per-ticket* structural gates that each take an `<id>` and verify a ticket is
*shaped* like dispatchable work — every type needs an `## Acceptance Criteria`
checklist. See the ticket template and gate reference in
[CLAUDE.md](CLAUDE.md#ticket-template-the-gates-enforce).)

**Links.** `rebar link <id1> <id2> <relation>` **requires** a relation; the six
relations are `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
`discovered_from`. `rebar unlink <source> <target>` takes **no** relation
argument — it is pair-scoped and removes the **most-recently-created** link
between that ordered pair, one per call, so to remove multiple links between the
same pair you call `unlink` repeatedly. Note that **blocking** links
(`blocks`/`depends_on`) may be promoted up the parent hierarchy when created (see
below), so `unlink` must target the **promoted (ancestor)** endpoint to remove
such a link.

Ticket work also leaves an **HMAC-signed attestation** — a machine-checkable proof
that a gate ran and that its verified steps are unaltered. For most projects this is
produced automatically by the code-review, plan-review, and completion-verifier
gates, so you never sign by hand. To sign manifests yourself or customize the
process, see [docs/manifest-signing.md](docs/manifest-signing.md).

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

**Running locally, offline, or read-only.** This auto-sync is configurable when you
don't want the store talking to a remote (the full key set and env names are in
[`docs/config.md`](docs/config.md)):

- **`sync.push`** (env `REBAR_SYNC_PUSH`) — `always` (default) pushes each write
  synchronously; `async` pushes in the background so per-write network latency
  doesn't serialize a batch; `off` keeps commits **local** and never pushes (`fsck`
  still surfaces `PUSH_PENDING`).
- **`sync.pull`** (env `REBAR_SYNC_PULL`) — `on` (default) lets reads fetch from the
  remote (the [freshness policy](#reads-share-one-freshness-policy-across-cli-library-and-mcp)
  below); `off` gives a pure-local replay (offline work, tight loops, or right after
  a write that already synced). Pass `--no-pull` to a single read subcommand for the
  same effect (e.g. `rebar list --no-pull`).
- **`mcp.readonly`** (env `REBAR_MCP_READONLY=1`) — serves only read tools over MCP,
  so no writes — and therefore no commits or pushes — happen at all.

How big can it get? Reads stay sub-second into the thousands of tickets; writes
are bounded by the per-event git commit (~25–30/s). See
[`docs/scale-envelope.md`](docs/scale-envelope.md) for representative measured
numbers, git-growth expectations, and the compaction/maintenance commands, and
[`docs/import-export.md`](docs/import-export.md) for bulk NDJSON export/import.

### Reads share one freshness policy across CLI, library, and MCP

Every **read** — `show`, `list`, `ready`, `search`, `deps` — first runs a
throttled (**≤1/min**), best-effort `git fetch` + reconverge of the local
`tickets` branch with `origin/tickets`, so a read reflects collaborators' pushes
within at most a minute. This is **one contract shared by all three interfaces**:
CLI, library (`rebar.list_tickets()`, …), and the MCP read tools all resolve
through a single read implementation. (Previously only CLI reads synced, leaving
MCP — the primary agent surface — with the *stalest* reads; that divergence is
gone.) To skip this fetch for a pure-local replay, set `sync.pull=off` or pass
`--no-pull` — see [Running locally, offline, or read-only](#the-store-auto-commits-and-auto-pushes-every-write)
above. Only the network fetch/merge is affected; the local reduce/cache path is
unchanged. See
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
ticket = rebar.show_ticket(tid)                 # TicketState
tickets = rebar.list_tickets(status="open")     # list[TicketState]
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

**Typed return contract.** The schema-backed `rebar.*` functions are annotated
with `TypedDict`s in [`rebar.types`](src/rebar/types.py) (e.g. `TicketState`,
`TransitionResult`, `ClaimResult`), so a type checker knows which keys a return
value carries. These are derived from the canonical JSON Schemas and describe the
*guaranteed* keys — returns stay plain `dict`s and the runtime shape is open
(extra keys may appear), so this is a floor, not a closed universe. Import them for
annotations/`TypedDict` access:

```python
from rebar.types import TicketState, TransitionResult

t: TransitionResult = rebar.transition(tid, "open", "in_progress")
```

**Stable exception surface.** `rebar.RebarError` (base) and its subclass
`rebar.ConcurrencyError` are the public exceptions. `RebarError` carries
`.returncode` (the underlying engine exit code) and `.stderr`; `ConcurrencyError`
(exit 10) means a status-dependent op (`transition`/`claim`/`reopen`) lost an
optimistic-concurrency race — re-read and retry, don't force. Catch `RebarError`
to handle any rebar failure uniformly.

**What's stable to depend on.** rebar is versioned 0.x; see
[docs/api-stability.md](docs/api-stability.md) for the per-surface stability
matrix (CLI, `--output json` schemas, the `rebar.*` facade, MCP tools, the event
wire format, and config keys) and what "may change before 1.0" means for each.

## MCP server

```bash
rebar-mcp          # stdio transport
```

Exposes ticket operations as MCP tools. `reconcile` defaults to `dry-run`
(`live` requires `REBAR_MCP_ALLOW_JIRA_SYNC=1`). Set `REBAR_MCP_READONLY=1`
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
                                           # false.
ticket.display_mode = "auto"               # auto | canonical | alias | short
compact.threshold   = 10
sync.push = "always"                       # always | async | off
sync.pull = "on"                           # on | off
mcp.readonly = false
scratch.base_dir = ""                      # default <repo>/.rebar/scratch
tracker.dir    = ".tickets-tracker"        # store worktree/symlink dir (env REBAR_TRACKER_DIR)
tracker.branch = "tickets"                 # orphan branch the event log lives on (env REBAR_TRACKER_BRANCH)
```

The full key set, the `REBAR_<KEY>` env names, and deprecation aliases are in
[`docs/config.md`](docs/config.md).

When the close gate is enabled, closing a story/epic requires a **certified
signature made at the current HEAD** — sign a manifest of verified steps
(`rebar sign <id> '[...]'`) then `rebar transition <id> closed`; re-sign if HEAD
moved, or bypass with `--force-close=<reason>`.

rebar keeps its writable state under `.rebar/` at the repo root. The `scratch`
store defaults to `<repo>/.rebar/scratch/` (override with `scratch.base_dir` /
`REBAR_SCRATCH_BASE_DIR`), and one-shot migration stamps are written under
`.rebar/` as well.

## Tests

Run the suite from an environment with the `[dev]` extra installed (a venv is
recommended); the interface-parity tests import the MCP server, so a bare
interpreter without the `mcp` extra will **error** rather than skip.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'                       # editable + pytest, mcp, ruff, mypy
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
