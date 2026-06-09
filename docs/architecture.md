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
     append+commit    ticket_reducer/            rebar_reconciler/
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
    identically over one store.

- **The engine** (`src/rebar/_engine/`) — the bash dispatcher (`rebar`, aliased
  `ticket`) routes subcommands to `ticket-*.sh` / `*.py` helpers. It must be
  installed UNPACKED to a real directory (no zipimport; `_engine.py:engine_dir()`
  asserts this).
  - **Write path** — all mutations go through the flock-guarded append+commit
    path (`ticket-lib.sh` `_flock_stage_commit`, `.ticket-write.lock`). The
    status-transition and `claim` critical sections live in `ticket_txn.py`
    (one process: lock → reduce+verify → write → commit; exit 10 on optimistic-
    concurrency mismatch).
  - **Reducer** (`ticket_reducer/`) — pure deterministic replay of the event log
    into compiled state; local rebuildable `.cache.json` per ticket.
  - **Graph** (`ticket_graph/`) — relations + cycle detection.
  - **Reconciler** (`rebar_reconciler/`) — level-triggered, bidirectional Jira
    sync; the one component with a grandfathered cross-client advisory lock
    (`.reconciler-pass-lock`, single-writer-by-design).

- **Storage** — a dedicated `tickets` git **orphan branch**, checked out as a
  worktree at `.tickets-tracker/`. Tickets are directories; mutations are
  append-only UUID-named event files (see `docs/event-schema.md`).

## Concurrency model (summary)

Every mutation is a new globally-unique append-only event; state is pure replay;
clients converge by **git merge-as-union + optimistic concurrency** — no
cross-client lock (except the grandfathered reconciler pass-lock). The full
invariants (I1–I9) and the sync/reconvergence algorithm are in
`docs/concurrency.md`; the agent-facing tool set and workflow are in
`CLAUDE.md`.
