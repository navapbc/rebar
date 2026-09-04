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

Run one ticket end-to-end with the CLI or the Python library; the JSON block is the
MCP server config so agents can drive the same loop over MCP. `rebar --help` (and
`rebar <command> --help`) is the authoritative command reference.

```bash
# CLI: one ticket through init -> create -> ready -> claim -> close
rebar init
tid=$(rebar create task "Add a login page" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
rebar ready                                              # lists it as ready to work
rebar claim "$tid"                                       # open -> in_progress
rebar transition "$tid" in_progress closed              # in_progress -> closed
```
```python
import rebar                                            # the same loop via the Python library
tid = rebar.create_ticket("task", "Add a login page")
rebar.claim(tid)
rebar.transition(tid, "in_progress", "closed")
```
```json
{ "mcpServers": { "rebar": { "command": "uvx", "args": ["--from", "nava-rebar[mcp]", "rebar-mcp"] } } }
```

That's the whole loop — **init → create → ready → claim → close**. The CLI and Python
blocks each drive **one** ticket end-to-end (the same id threaded through every step, no
hard-coded id); the JSON is the MCP server config — add it to your client so an agent can
run the same loop via the MCP tools. State is shared through the repo so many agents (and
teammates via Jira) coordinate without stepping on each other.

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
- **LLM review gates** *(optional).* Review an agent's plan before work starts, its completion before the ticket closes, and its code before it merges. Plan-review and code-review share one four-pass kernel. A finder cites evidence, a separate verifier tests each claim with atomic yes or no questions, and deterministic policy decides what blocks. A passing plan or completion review records an operation certificate as a DSSE envelope. The envelope carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. The certificate names that environment as its principal. The result is a machine-checkable process record for the reviewed work.
- **Provenance links** — `discovered_from` ties emergent work back to the ticket
  that surfaced it.
- **One store, three interfaces** — drive it from the CLI, a Python library, or
  the MCP server.

## Requirements

**System prerequisites:**
- [Python](https://www.python.org) ≥ 3.11
- [`git`](https://git-scm.com) is a runtime prerequisite because the store uses a Git orphan branch and worktree. The runtime engine uses in-process Python and does not require `bash` or `jq`.
- Git 2.38 or newer is required for development, tests, and CI. The floor is declared in `.github/git-version-floor.txt`. The test suite and CI fail when Git is below that floor.
- Deployments that use the optional S3 repair path require Git 2.38 or newer because that path invokes `git merge-tree --write-tree`.
- **No external lock binary is required.** Write serialization uses a two-window
  lock built entirely from the Python standard library — a `fcntl.flock(LOCK_EX)`
  advisory lock plus an atomic `mkdir` lock (`src/rebar/_store/lock.py`) — so there
  is **no dependency on util-linux's `flock` binary** (or any other external tool).
  The `mkdir` window keeps mutual exclusion holding even where `fcntl.flock` is
  unreliable (e.g. some network filesystems).
- [`acli`](https://developer.atlassian.com/cloud/acli/) (Atlassian CLI) — a
  **required external binary for the Jira Cloud reconciliation/bridge path**: every
  `bridge`/`reconcile` Cloud mutation shells out to it, so it must be installed on
  `PATH` (install pointer: [docs/jira-sync-setup.md](docs/jira-sync-setup.md)). The
  `[jira-datacenter]` Data Center path does **not** need it — it uses the `jira`
  Python library.

**Python dependencies.** A base install (`pip install nava-rebar`) provides the `rebar` CLI, the `import rebar` library, and the lean workflow engine. It installs [`pyyaml>=6`](https://pyyaml.org) for the workflow DSL loader, [`jsonschema>=4.18`](https://python-jsonschema.readthedocs.io) for schema registry and workflow input and output validation, and [`referencing>=0.30`](https://referencing.readthedocs.io) for JSON Schema `$ref` resolution. The engine core and reconciler otherwise use the Python standard library. All other dependencies are optional extras that are imported lazily to keep the base installation light. CI verifies this boundary.

- **Optional runtime capabilities** — install what you serve:
  - **`[mcp]`** installs the [`rebar-mcp` server](https://modelcontextprotocol.io) with `mcp>=1.28.1,<2` and `pyjwt[crypto]>=2.10,<3`.
  - **`[agents]`** — the LLM agent-operations framework + agentic workflow steps
    (`rebar review-plan`, the `code_review` workflow): the provider-agnostic
    [pydantic-ai](https://ai.pydantic.dev) runtime (`pydantic-ai-slim[anthropic]`)
    plus [`json-repair`](https://github.com/mangiucugna/json_repair).
- **Development & authoring extras** — not needed to run or serve rebar:
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
fits. (System prerequisites in all cases: `git` (≥ 2.38 to develop/test rebar; see
[Requirements](#requirements)) and `python3` (≥ 3.11); write
serialization uses a built-in `fcntl.flock` + `mkdir` lock with no external
binary; `acli` is required on `PATH` for the Jira Cloud reconciliation/bridge path
(see [docs/jira-sync-setup.md](docs/jira-sync-setup.md)), while the
`[jira-datacenter]` Data Center path does not need it.)

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
pip  install nava-rebar              # library: import rebar  (runtime deps: pyyaml, jsonschema, referencing)
pip  install 'nava-rebar[mcp]'       # + MCP server: rebar-mcp
pip  install 'nava-rebar[agents]'    # + LLM agent ops + agentic workflow steps (rebar.llm)
pip  install 'nava-rebar[tracing]'   # + OTLP trace sink (write-only)
pip  install 'nava-rebar[agents,tracing]'        # the union, if you want it all
```

The base install runs **scripted** workflows (`rebar workflow new/validate/show/run`)
with no extra; **agentic** workflow steps and `rebar review-plan` need `[agents]`. Authoring
a workflow **visually** — `rebar workflow edit <file>`, a local bpmn-js editor that
round-trips the diagram back to the IR — also needs no extra and no Node/npm: the editor
front-end ships pre-built in the wheel and is served locally (no CDN). For what the
engine is *for* — when to author a workflow vs a bespoke op, the YAML DSL, the
three-pass review pattern, and the prompt-library + eval seam — see
[docs/workflow-engine.md](docs/workflow-engine.md); for visual editing specifically see
[docs/workflow-editor.md](docs/workflow-editor.md).

The `[agents]` extra adds the optional **LLM agent operations framework** (`rebar.llm`). It provides tool-using agents that review tickets and code through the library, CLI, and MCP interfaces. Model classes are configured in `[tool.rebar.llm.model_classes]` or with `REBAR_LLM_<CLASS>_MODEL`, `REBAR_LLM_MODEL_PROVIDER`, and `REBAR_LLM_BASE_URL`. The single `REBAR_LLM_MODEL` variable is deprecated. See [docs/llm-framework.md](docs/llm-framework.md).

The `[agents]` extra installs the Anthropic provider used for Claude. Bedrock requires `nava-rebar[agents,bedrock]`. ChatGPT and OpenAI-compatible endpoints require `[agents]` plus `pydantic-ai-slim[openai]`. Gemini requires `[agents]` plus `pydantic-ai-slim[google]`. Core rebar never requires or imports the LLM stack unless an agent operation is used.

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
mutating bridge tools (`bridge_run`, `bridge_sync`, `bridge_pause`, and
`bridge_resume`) require `REBAR_MCP_ALLOW_JIRA_SYNC=1`. Both flags accept any
case-insensitive truthy value — `1`, `true`, or `yes` (surrounding whitespace
tolerated); anything else (incl. unset) is off.

#### Private-repo fetch credentials (code-reading gates)

The LLM code-reading gates `review_plan`, `verify_completion`, `review_code`, and `scan_spec` default to attested mode. They fetch the selected ref from `origin` and read an immutable snapshot at the pinned SHA. A server whose `REBAR_ROOT` points to a private repository therefore needs read credentials through a Git credential helper, deploy key, or token in the server clone. Without credentials, attested mode fails closed with a remediation message and disables terminal prompts through `GIT_TERMINAL_PROMPT=0`. `source=local` reads the in-place checkout without fetching and never signs the result. [The snapshot guide](docs/repo-snapshot-gates.md) documents these semantics, the operation-certificate trust model for DSSE envelopes that carry SSHSIG signatures over their PAE bytes, each produced with an environment's Ed25519 key and attributed to that environment, and the settings for temporary storage, disk-space thresholds, and EFS or NFS locking.

### From source

```bash
git clone https://github.com/navapbc/rebar && cd rebar
pip install .              # library + CLI (runtime deps: pyyaml, jsonschema, referencing)
pip install '.[mcp]'      # + MCP server (FastMCP)
# Developing rebar itself — the full dev environment (test/lint/type tooling +
# the agents stack so the LLM validation tests RUN, not skip), installed through
# the committed uv.lock (or `make install`, which adds the pre-commit gate):
uv sync --extra dev
```

> **pipx source installs and older uv.** If `pipx install "<path>[agents]"` stops
> before installing rebar with `pipx needs uv>=0.9.17`, it selected an older host uv.
> Retry the same source install with pipx's pip backend:
>
> ```bash
> pipx install --backend pip "<path>[agents]"
> ```
>
> This is a host pipx/uv toolchain mismatch, not a rebar packaging defect.

> **Contributing changes?** GitHub is a **read-only mirror** — `main` only advances via
> Gerrit's two-vote gate (`LLM-Review` + `Verified`/CI). New contributors: start with the
> friendly walkthrough [docs/your-first-change.md](docs/your-first-change.md); the full
> reference is [CONTRIBUTING.md](CONTRIBUTING.md) (clone from Gerrit, push to
> `refs/for/main`, then land it with a plain Gerrit **Submit** once both votes pass — `main`
> is Rebase-If-Necessary, so Gerrit rebases onto the tip and submits server-side).

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

The **complete, always-current command reference** for every subcommand is [docs/cli-reference.md](docs/cli-reference.md). It is generated from the CLI's own help data. The essentials are `rebar init` → `rebar create <type> "<title>"` → `rebar ready` → `rebar claim <id>` → `rebar transition <id> <current> <target>`.

Run `rebar help`, `rebar --help`, or `rebar -h` for the subcommand overview. Run `rebar <subcommand> --help` or `rebar help <subcommand>` for a specific subcommand. For leaf commands, an exact `--help` or `-h` token in any position before `--` prints usage without executing the command. Nested command families retain child routing. A leading help flag prints family help, while forms such as `rebar bridge preview --help` and `rebar audit show --help` print child help.

Repo root is resolved from `REBAR_ROOT`, falling back to the git toplevel of the
working directory.

**Structured output.** Every data-returning command emits machine-readable JSON
via the canonical `--output json` flag (short `-o json`; `--output llm` gives a
token-minified shape for `show`/`list`/`ready`). Each distinct JSON shape is
documented by a JSON Schema and validated across the CLI, library, and MCP in CI.
See [docs/output-schemas.md](docs/output-schemas.md) for the per-command contract
and the schema source-of-truth.

**Claiming work.** `rebar claim <id>` atomically moves an open ticket to `in_progress`. When `--assignee` is omitted, `claim` uses `ticket.default_assignee`. An explicit `--assignee` overrides that setting and must be a Jira-resolvable email or accountId.

**Repo-wide health with `validate`.** `rebar validate` takes **no ticket id** — it
scans the whole store and prints an overall tracker-health score (1-5, exit 0-4)
bucketed into critical / major / minor / warning findings (`--output json`,
`--terse`, `--verbose`). Passing it a ticket id errors. (rebar also has
*per-ticket* structural gates that each take an `<id>` and verify a ticket is
*shaped* like dispatchable work — every type needs an `## Acceptance Criteria`
checklist. See the ticket template and gate reference in
[docs/plan-review-criteria-guide.md](docs/plan-review-criteria-guide.md).)

**Closing work.** `rebar transition <id> in_progress closed` closes a task, bug, story, or epic. A bug close also requires `--class`. Completion verification is optional and disabled by default. Set `verify.require_completion_verification_for_close = true` to run it as part of each ordinary work-ticket close before the status change is recorded.

**Links.** `rebar link <id1> <id2> <relation>` requires one of seven relations. They are `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from`, and `caused_by`. `rebar unlink <source> <target> [relation]` accepts an optional relation. Without a relation, it removes the most recently created active link for that ordered pair. With a relation, it removes exactly that relation and preserves other relations between the pair. Blocking links may be promoted up the parent hierarchy when created, so `unlink` must target the promoted ancestor endpoint.

A passing plan-review or completion-verifier records an **operation certificate**. The certificate is a DSSE envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. Its principal identifies that environment. The code-review gate reports its verdict through the review system and does not create this ticket certificate. The `rebar sign` command and library surface can also attach a manifest certificate outside those two gates. See [docs/manifest-signing.md](docs/manifest-signing.md).

### Hierarchy promotion of blocking links

For **blocking** dependencies only (`blocks`, `depends_on`), rebar promotes the link endpoints up the parent hierarchy so the dependency sits between tickets at a comparable level (epic↔epic, story↔story, task/bug↔task/bug). When it does so, it emits a `REDIRECT: A→B promoted to …` note. Non-blocking relations (`relates_to`, `duplicates`, `supersedes`, `discovered_from`, `caused_by`) are linked exactly as given, with no promotion.

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

preview = rebar.bridge_preview(only=[tid])       # typed, non-mutating Jira plan
run = rebar.bridge_run(profile="dry-run")         # captured scheduled-run result; prints nothing
sync = rebar.bridge_sync(max_changes=10)         # typed, explicitly mutating sync
status = rebar.bridge_status(max_age_seconds=3600)
# Durable operator controls and the live six-step capability check:
rebar.bridge_pause("maintenance")
rebar.bridge_resume()
access = rebar.bridge_check_access()

# Legacy reconcile(mode=...) is not a Python API; use explicit bridge operations.
audit = rebar.bridge_fsck()                       # offline bridge audit

# Sign a DSSE operation certificate by applying SSHSIG to its PAE bytes with the environment's Ed25519 key and principal.
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

Exposes ticket operations as MCP tools. The **complete tool reference**, grouped by
gate tier (read-only / LLM-gated / write-gated), is
[docs/mcp-reference.md](docs/mcp-reference.md) (generated from the server's own
registrars). Use the explicit `bridge_preview`, `bridge_run`, `bridge_sync`, `bridge_status`,
`bridge_pause`, `bridge_resume`, `bridge_check_access`, and `bridge_fsck` tools.
The former MCP `reconcile(mode=...)` compatibility tool is no longer registered.
Run/sync/pause/resume require `REBAR_MCP_ALLOW_JIRA_SYNC=1`;
`REBAR_MCP_READONLY=1` blocks every mutation. To
register it in an MCP client (registry name
`io.github.navapbc/rebar`, or a direct `uvx` config), see
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
verify.require_completion_verification_for_close = true  # gate work-ticket close on a PASS
                                           # completion verdict (signed onto the ticket);
                                           # fail-closed. Default false.
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

When the close gate is enabled, a close transition runs the completion verifier against the selected code ref. A passing verifier records a DSSE operation-certificate envelope that carries an SSHSIG signature over its PAE bytes, produced with the signing environment's Ed25519 key. The certificate principal identifies that environment. Run the transition again if the verified code changes. `--force=<reason>` bypasses the gate and records no certificate.

rebar keeps its writable state under `.rebar/` at the repo root. The `scratch`
store defaults to `<repo>/.rebar/scratch/` (override with `scratch.base_dir` /
`REBAR_SCRATCH_BASE_DIR`), and one-shot migration stamps are written under
`.rebar/` as well.

## Tests

Run the suite from an environment with the `[dev]` extra installed (a venv is
recommended); the interface-parity tests import the MCP server, so a bare
interpreter without the `mcp` extra will **error** rather than skip.

```bash
uv sync --extra dev && source .venv/bin/activate  # locked install: pytest, mcp, ruff, mypy
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
