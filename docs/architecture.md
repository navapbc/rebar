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
                 src/rebar/_engine/  (bash dispatcher + python helpers)
                            │
              ┌─────────────┼───────────────────────────┐
              ▼             ▼                            ▼
     append+commit    rebar.reducer              rebar_reconciler/
     (locked write     (pure replay → state)      (Jira bidirectional sync)
      path, I5)
                            │
                            ▼
        git: tickets orphan branch  ·  worktree at .tickets-tracker/
```

## Components

- **The three interfaces** are thin layers over one engine:
  - **CLI** (`src/rebar/cli.py`) — a pass-through to the bash dispatcher; intercepts
    `reconcile` to route it to `python -m rebar_reconciler`.
  - **Library** (`src/rebar/__init__.py`) — typed functions that subprocess the
    dispatcher (`_run`) and map exit codes to exceptions (notably exit 10 →
    `ConcurrencyError`); plus native in-process reads (`reduce_ticket` /
    `reduce_all_tickets` via `_native.py`).
  - **MCP server** (`src/rebar/mcp_server.py`) — FastMCP tools built on the library;
    write tools gated by `REBAR_MCP_READONLY`; `reconcile` defaults to dry-run.
  - The interface-parity tier (`tests/interfaces/`) asserts all three behave
    identically over one store, and that every structured output conforms to its
    canonical JSON Schema (`src/rebar/schemas/`) — the machine-readable **output
    contract**, documented in [output-schemas.md](output-schemas.md). One flag
    (`--output`/`-o`) selects it; its parsing lives once in
    `_engine/ticket_output.py` (no duplicate bash/Python logic).

- **The engine** (`src/rebar/_engine/`) — the bash dispatcher (`rebar`, aliased
  `ticket`) routes subcommands to `ticket-*.sh` / `*.py` helpers. It must be
  installed UNPACKED to a real directory (no zipimport; `_engine.py:engine_dir()`
  asserts this).
  - **Write path** — all mutations go through the flock-guarded append+commit
    path (`ticket-lib.sh` `_flock_stage_commit`, `.ticket-write.lock`). The
    status-transition and `claim` critical sections live in `ticket_txn.py`
    (one process: lock → reduce+verify → write → commit; exit 10 on optimistic-
    concurrency mismatch).
  - **Reducer** (`rebar.reducer`, code at `src/rebar/reducer/`) — pure
    deterministic replay of the event log into compiled state; local rebuildable
    `.cache.json` per ticket.
  - **Graph** (`rebar.graph`, code at `src/rebar/graph/`) — relations + cycle
    detection.
  - **Reconciler** (`rebar_reconciler/`, in the engine dir) — level-triggered,
    bidirectional Jira sync; the one component with a grandfathered cross-client
    advisory lock (`.reconciler-pass-lock`, single-writer-by-design).

### Python package layout & the engine import boundary

The library, CLI, and MCP server are the `rebar` package; the engine ships as
package **data** under `rebar/_engine/` (bash + `*.py` helpers exec'd as real
files). Two import worlds meet at this boundary, and the rule (ticket
`fare-rant-clasp`, Rec 5) is **the in-process library path never puts a generic
top-level name on `sys.path`**:

- **In-process (library / MCP reads).** The replay engine is real subpackages:
  `rebar.reducer`, `rebar.graph`, and the in-process read surface
  (`rebar._engine_support.{reads,resolver,output}`). `_native.py` / `_reads.py`
  import these directly — no `sys.path` insertion of the engine dir, so after
  `import rebar` a bare `import ticket_reducer` fails (guarded by
  `tests/unit/test_engine_dir.py::test_library_path_exposes_no_generic_top_level_engine_names`).
- **Subprocess (bash dispatcher + `python3` helpers).** `engine_env()` is the
  ONE place the engine dir goes on an import path, and it is scoped to
  subprocesses. It puts both the engine dir and the `rebar` package parent on
  `PYTHONPATH`, so the engine's bare `python3` resolves the old top-level names —
  now thin **compat shims** in `rebar/_engine/` (`ticket_reducer/`,
  `ticket_graph/`, `ticket_reads.py`, `ticket_resolver.py`, `ticket_output.py`)
  that re-export the `rebar.*` subpackages. Each shim does
  `sys.modules[__name__] = <real module>`, so `ticket_reducer is rebar.reducer`
  (one object, one cache); shims that bash also runs as scripts
  (`ticket_output.py` via `ticket-output.sh`) forward `__main__` to the real
  module. These shims exist only until the bash→Python strangler-fig ports
  (`adult-oxide-slave`) drop the old import names.

The write core `ticket_txn.py` is invoked by absolute path from bash and is never
imported in-process, so it stays in the engine dir (no library-path exposure to
remove). The **reconciler** (`rebar_reconciler/`) likewise stays in the engine
dir: the library only ever reaches it as a subprocess (`python -m
rebar_reconciler`) or by loading a single file by path (`mode.py` in
`mcp_server.py`), never as an in-process package import — so it leaks no generic
name onto the library path. Its internal `sys.modules` identity keys (the
`spec_from_file_location` dotted-key scheme that copes with the test-package
shadow and `acli-integration.py`'s hyphen) are **left as-is by deliberate
decision**; turning them into ordinary imports is owned by `tangly-abbey-smelt`,
which is sequenced after this repackage precisely because the package is now
importable as `rebar.*` for it to build on.

- **Storage** — a dedicated `tickets` git **orphan branch**, checked out as a
  worktree at `.tickets-tracker/`. Tickets are directories; mutations are
  append-only UUID-named event files (see `docs/event-schema.md`). Every write
  auto-commits its event **and** auto-pushes `tickets` to `origin/tickets` when an
  `origin` remote exists, so local ticket activity is shared with the remote
  immediately (best-effort; see `docs/concurrency.md` "Outbound — push").

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
- **Prefer deleting bash over splitting it.** An oversized `*.sh` should be retired
  via the bash→Python strangler-fig migration (ticket `adult-oxide-slave`), not
  carved into more bash.

### Current offenders (> 800 LOC) and planned remedy

Tracked so the over-cap set is visible, not silently growing. A warn-only size
report runs in CI (`.github/workflows/test.yml`) so new offenders surface in PRs.

| File | LOC | Remedy |
|------|----:|--------|
| `rebar_reconciler/applier.py` | ~3480 | split along seams — ticket `tangly-abbey-smelt` |
| `_engine/ticket-lib-api.sh` | ~2370 | retire via strangler-fig — ticket `adult-oxide-slave` |
| `_engine/acli-integration.py` | ~2180 | split the Jira-client vs ADF concerns (reconciler) |
| `_engine/ticket-lib.sh` | ~2000 | retire via strangler-fig — `adult-oxide-slave` |
| `rebar_reconciler/reconcile.py` | ~1320 | split orchestration vs pass-driver seams |
| `rebar_reconciler/outbound_differ.py` | ~1130 | split per-field differ seams |
| `_engine/ticket-next-batch.sh` | ~950 | retire via strangler-fig — `adult-oxide-slave` |
| `_engine/validate-issues.sh` | ~945 | retire via strangler-fig — `adult-oxide-slave` |

Files in the 500–800 band (`_advisory_lock.py`, `differ.py`, `inbound_differ.py`,
`ticket_reads.py`, `__init__.py`, `ticket-link.sh`, `ticket-bridge-fsck.py`) are at
the ceiling, not over it — watch, don't split preemptively.
