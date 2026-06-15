# rebar architecture

rebar is an event-sourced ticket system + Jira reconciler, exposed three ways
over one git-backed store.

```
        ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
        │  CLI: rebar │   │ library: rebar│   │ MCP: rebar-mcp   │
        └──────┬──────┘   └──────┬───────┘   └────────┬─────────┘
               │                 │                    │
               └────────────┬────┴────────────────────┘
                            ▼
            in-process Python core (src/rebar/: _cli · _commands ·
            _store · reducer · graph · _engine_support)
                            │
              ┌─────────────┼───────────────────────────┐
              ▼             ▼                            ▼
     append+commit    rebar.reducer              rebar_reconciler/
     (locked write     (pure replay → state)      (Jira bidirectional sync;
      path, I5)                                    runs as a subprocess)
                            │
                            ▼
        git: tickets orphan branch  ·  worktree at .tickets-tracker/
```

## Components

- **The three interfaces** are thin layers over one in-process core:
  - **CLI** (`src/rebar/cli.py` → `rebar._cli`) — an in-process argparse CLI that
    routes each subcommand to its in-process handler; intercepts `reconcile` to
    route it to `python -m rebar_reconciler`.
  - **Library** (`src/rebar/__init__.py`) — typed in-process functions over
    `rebar._commands` / `rebar.reducer` / `rebar.graph`, mapping the write path's
    exit 10 to `ConcurrencyError`; in-process reads via `_native.py` / `_reads.py`.
  - **MCP server** (`src/rebar/mcp_server.py`) — FastMCP tools built on the library;
    write tools gated by `REBAR_MCP_READONLY`; `reconcile` defaults to dry-run.
  - The interface-parity tier (`tests/interfaces/`) asserts all three behave
    identically over one store, and that every structured output conforms to its
    canonical JSON Schema (`src/rebar/schemas/`) — the machine-readable **output
    contract**, documented in [output-schemas.md](output-schemas.md). One flag
    (`--output`/`-o`) selects it; its parsing lives once in
    `rebar._engine_support.output`.

- **The in-process core** (`src/rebar/`) — every subcommand and the library/MCP
  reads & writes run in Python: `_cli` (argparse routing), `_commands` (leaf
  writes, lifecycle `transition`/`reopen`/`claim`, compaction, scratch, delete,
  init, fsck), `_engine_support` (reads, gates, lookups, descendants, validate,
  bridge), `_store` (the locked write core), and `reducer` / `graph`.
  - **Write path** — all mutations go through ONE locked append+commit path in
    `rebar._store`: `lock.py` (the unified fcntl+mkdir dual-leg lock on
    `.ticket-write.lock`), `event_append.py` (canonical commit), `push.py`,
    `sync.py`. The status-transition and `claim` critical sections live in
    `rebar._commands.txn` (one process: lock → reduce+verify → write → commit;
    exit 10 on optimistic-concurrency mismatch); they, compaction, and the
    reconciler-inbound writer all acquire the same `rebar._store.lock`.
  - **Reducer** (`rebar.reducer`, code at `src/rebar/reducer/`) — pure
    deterministic replay of the event log into compiled state; local rebuildable
    `.cache.json` per ticket.
  - **Graph** (`rebar.graph`, code at `src/rebar/graph/`) — relations + cycle
    detection.
  - **Reconciler** (`rebar_reconciler/`, shipped as `_engine/` package data) —
    level-triggered, bidirectional Jira sync, launched as a subprocess
    (`python -m rebar_reconciler`); the one component with a grandfathered
    cross-client advisory lock (`.reconciler-pass-lock`, single-writer-by-design).

- **LLM agent operations** (`rebar.llm`, code at `src/rebar/llm/`) — an OPTIONAL
  framework for tool-using LLM agents that emit structured findings, exposed over
  library/CLI (`rebar review`)/MCP (`review_ticket`). The engine core stays
  stdlib-only; everything here is behind the `nava-rebar[agents]` extra and
  lazy-imported. A pluggable `Runner` (default in-process LangChain/LangGraph for
  review; an opt-in deepagents harness for future task types; a Langflow REST stub;
  a `FakeRunner` for tests) runs the agent with read-only repo file tools + MCP
  tools; output is constrained to the `review_result` JSON Schema.
  Langfuse provides tracing + the reviewer-prompt library. See
  [llm-framework.md](llm-framework.md).

### Python package layout & the engine import boundary

The library, CLI, MCP server, and all command/read/write logic are the `rebar`
package, in-process. The `rebar/_engine/` directory ships as package **data**
holding the genuine subprocess tooling: the `rebar_reconciler` package,
`jira-capability-probe.py`, and the alias `resources/` wordlist. The rule (ticket
`fare-rant-clasp`, Rec 5) is **the in-process library path never puts a generic
top-level name on `sys.path`**:

- **In-process (everything but the reconciler + probe).** The replay engine and
  the read/write surface are real subpackages: `rebar.reducer`, `rebar.graph`,
  `rebar._commands`, `rebar._store`, and `rebar._engine_support.*` (reads,
  resolver, output, gates, …). Nothing inserts the engine dir onto `sys.path`, so
  after `import rebar` a bare `import rebar_reconciler` (or any `_engine/` module)
  fails — guarded by
  `tests/unit/test_engine_dir.py::test_library_path_exposes_no_generic_top_level_engine_names`.
- **Subprocess (the reconciler + Jira probe).** `engine_env()` is the ONE place
  the engine dir goes on an import path (`PYTHONPATH`), scoped to the subprocess
  launches `python -m rebar_reconciler` and `jira-capability-probe.py`, so the
  top-level `rebar_reconciler` package resolves there. It also pins `REBAR_ROOT`/
  `PROJECT_ROOT`, the alias wordlist path, and `REBAR_TICKET_CLI` — the in-process
  `rebar` CLI the reconciler and `validate` read tickets through
  (`rebar._engine.in_process_cli`).

The **reconciler** (`rebar_reconciler/`) stays in the engine dir: the library only
ever reaches it as a subprocess (`python -m rebar_reconciler`) or by loading a
single file by path (`mode.py` in `mcp_server.py`), never as an in-process package
import — so it leaks no generic name onto the library path. ACLI integration lives
at `rebar_reconciler/acli.py`, reached via ordinary `from rebar_reconciler import
acli` package imports.

- **Storage** — a dedicated `tickets` git **orphan branch**, checked out as a
  worktree at `.tickets-tracker/`. Tickets are directories; mutations are
  append-only UUID-named event files (see `docs/event-schema.md`). Every write
  auto-commits its event **and** auto-pushes `tickets` to `origin/tickets` when an
  `origin` remote exists, so local ticket activity is shared with the remote
  immediately (best-effort; see `docs/concurrency.md` "Outbound — push").

- **Init vs. symlink (two distinct concepts).** *Initializing* a store materializes
  the orphan `tickets` branch + the linked worktree and edits `.git/info/exclude`
  — it mutates the host repo, so it requires consent (an interactive `[Y/n]`
  confirmation, or an explicit `rebar init` / `rebar.init_repo`); it is never done
  silently in automation. *Symlinking* is different: when the host repo is itself a
  linked git worktree whose MAIN repo is already initialized, `init` just creates a
  `.tickets-tracker` symlink to the main repo's store. That only adds a local link
  to an EXISTING store and leaves the underlying repo untouched, so the auto-init
  gate creates it **automatically, without a prompt** — even non-interactively. The
  discriminator is `rebar._commands.init.pending_init_is_symlink`; the gate lives in
  `rebar._cli._init` (`_create_tracker`). Writes from a worktree still serialize on
  the main store: the write lock resolves the symlink via `realpath` so the
  symlinked and real-path callers contend on the same lock file.

## Concurrency model (summary)

Every mutation is a new globally-unique append-only event; state is pure replay;
clients converge by **git merge-as-union + optimistic concurrency** — no
cross-client lock (except the grandfathered reconciler pass-lock). The full
invariants (I1–I9) and the sync/reconvergence algorithm are in
`docs/concurrency.md`; the agent-facing tool set and workflow are in
`CLAUDE.md`.

## Module-size policy

rebar is built to be edited by agents, which read a unit whole. The balance is
between *editability* (a file an agent can load and reason about in one pass) and
*fragmentation* (so many tiny files that following a change means chasing imports).
The policy:

- **Target 200–500 LOC** per unit; a unit is one cohesive responsibility.
- **Soft cap 800 LOC.** Over 800 is a smell to address — but only by a *real*
  split, never a mechanical one.
- **Split only along call-graph seams that already exist** — extract a cluster of
  functions that already call each other and little else. Do not split a unit just
  to hit a line count.
- **Never create files < 100 LOC by splitting.** Two 60-line files that always
  change together are worse than one 120-line file.

### Current offenders (> 800 LOC) and planned remedy

Tracked so the over-cap set is visible, not silently growing. A warn-only size
report runs in CI (`.github/workflows/test.yml`) so new offenders surface in PRs.

| File | LOC | Remedy |
|------|----:|--------|
| `rebar_reconciler/reconcile.py` | ~1305 | split orchestration vs pass-driver seams |
| `rebar_reconciler/outbound_differ.py` | ~1114 | split per-field differ seams |
| `__init__.py` | ~830 | library facade just over the cap — split the read vs write API if it grows |

Files in the 500–800 band (`_engine_support/reads.py`, `_commands/transition.py`,
`_commands/composer.py`, `_engine_support/next_batch.py`, `mcp_server.py`, and
several `rebar_reconciler/` modules — `apply_inbound.py`, `applier.py`,
`_advisory_lock.py`, `acli.py`, `inbound_differ.py`, `differ.py`,
`batch_dispatch.py`, `acli_cli_ops.py`) are at the ceiling, not over it — watch,
don't split preemptively.
