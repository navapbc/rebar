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
    `.cache.json` per ticket. `reduce_all_tickets()` is the single batch-compile
    that backs `search`/`list`/`ready`/`next_batch`/`deps`/`validate`; its
    `exclude_session_logs` flag is the **compile-exclusion seam** — the
    graph/health hot paths and default `list` set it so verbose `session_log`
    bodies never tax those compiles, while `search` and single-ticket `show`
    leave it off so logs stay discoverable (see CLAUDE.md "Session logs").
  - **Graph** (`rebar.graph`, code at `src/rebar/graph/`) — relations + cycle
    detection. Excludes `session_log` tickets from the dependency graph (they
    carry non-blocking links only and never block/unblock work); `deps` on a
    `session_log` itself still resolves its own links.
  - **Reconciler** (`rebar_reconciler/`, shipped as `_engine/` package data) —
    level-triggered, bidirectional Jira sync, launched as a subprocess
    (`python -m rebar_reconciler`); the one component with a grandfathered
    cross-client advisory lock (`.reconciler-pass-lock`, single-writer-by-design).
  - **Import/export** (`rebar._io`, code at `src/rebar/_io/`) — NDJSON
    export/import of ticket state backing `rebar.export_tickets`/`import_tickets`,
    the CLI `export`/`import` subcommands, and the MCP equivalents (see
    [import-export.md](import-export.md)). Export streams replay-derived states;
    import is idempotent (re-importing the same stream is a no-op).

- **LLM agent operations** (`rebar.llm`, code at `src/rebar/llm/`) — an OPTIONAL
  framework for tool-using LLM agents that emit structured findings, exposed over
  library/CLI (`rebar review`)/MCP (`review_ticket`). The engine core needs NO LLM
  dependency (its only runtime deps are `pyyaml`, the workflow DSL loader, and
  `jsonschema`, the schema-registry/contract validator); everything
  here is behind the `nava-rebar[agents]` extra and lazy-imported. A pluggable `Runner`
  (the in-process, provider-agnostic pydantic-ai runtime; a `FakeRunner` for tests)
  runs the agent with read-only repo file tools + MCP
  tools; output is constrained to the `review_result` JSON Schema.
  Langfuse is the optional OTLP tracing endpoint (`[tracing]` extra); reviewer prompts are
  git-canonical (packaged `reviewers/*.md` or project `.rebar/prompts/`). See
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
  top-level `rebar_reconciler` package resolves there. It also pins `REBAR_ROOT`
  (the single repo-root override). The alias wordlist and the in-process ticket-reader CLI are NOT
  pinned — subprocesses self-resolve them (`rebar.reducer._alias` resolves the
  bundled wordlist; the reconciler and `validate` call `rebar._engine.in_process_cli`).

The **reconciler** (`rebar_reconciler/`) stays in the engine dir: the library only
ever reaches it as a subprocess (`python -m rebar_reconciler`) or by loading a
single file by path (`mode.py` in `mcp_server.py`), never as an in-process package
import — so it leaks no generic name onto the library path. ACLI integration lives
at `rebar_reconciler/acli.py`, reached via ordinary `from rebar_reconciler import
acli` package imports.

The **workflow visual editor** front-end is another piece of vendored package data:
`rebar/llm/workflow/editor_assets/` is an npm project (bpmn-js + properties panel; the
diagram layout is generated by the Python serializer) whose **built** bundle
`dist/editor.{js,css}` is committed and shipped, and
served locally by `editor.py` (no CDN, no runtime npm — the Python side stays stdlib).
Node/npm are needed only to *rebuild* that bundle or to run the faithful editor E2E tier
(`tests/e2e/`); both are developer-only and off the client/runtime path. See
[docs/workflow-editor.md](workflow-editor.md).

The workflow engine's hardest assumption — that the thin interpreter can resume
exactly-once across every crash point — was de-risked up front by
[`engine_interpreter_poc.py`](experiments/workflow-remediation-pocs/engine_interpreter_poc.py);
that and the other workflow-engine-v2 de-risk POCs are indexed in
[docs/experiments/workflow-remediation-pocs/README.md](experiments/workflow-remediation-pocs/README.md).

- **Storage** — a dedicated `tickets` git **orphan branch**, checked out as a
  worktree at `.tickets-tracker/`. Tickets are directories; mutations are
  append-only UUID-named event files (see `docs/event-schema.md`). Every write
  auto-commits its event **and** auto-pushes `tickets` to `origin/tickets` when an
  `origin` remote exists, so local ticket activity is shared with the remote
  immediately (best-effort; see `docs/concurrency.md` "Outbound — push"). The
  branch name and the worktree/symlink dir shown here are the **defaults**; both are
  configurable via `tracker.branch` / `tracker.dir` (resolved through `tickets_branch()`
  / `tracker_dir()`), set at `init` and not auto-migrated thereafter — see
  [`docs/config.md`](config.md).

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

Tracked so the over-cap set is visible, not silently growing. A CI **module-size
gate** (`.github/workflows/test.yml`) **fails the build** when a file exceeds 800
LOC and is not in `.github/module-size-allowlist.txt` — so a *new* offender cannot
land silently. **`.github/module-size-allowlist.txt` is the source of truth for the
over-cap set** (and the live LOC: `wc -l` the file, or read the CI gate output);
exact line counts are deliberately **not** duplicated here — they drift every commit
and a stale number is worse than none. This table mirrors the allowlist's membership
and records the **planned remedy seam** per file; adding a file to the allowlist
requires a row here.

| File | Remedy |
|------|--------|
| `rebar_reconciler/reconcile.py` | decompose the ~750-LOC `reconcile_once` into named in-place phase helpers (corrupt-snapshot abort, OM→Mutation conversion); the natural sub-blocks are below the 100-LOC file floor, so prefer in-place helpers over a sibling module |
| `rebar_reconciler/outbound_differ.py` | extract the comment-diff cluster (`_diff_comments` + its `_normalize_comment_body`/`_decorate_outbound_comment`/`_map_comments_for_create`/`_is_machine_marker_comment`/`_load_comment_limits`/`_load_adf` satellites) to a sibling `outbound_comments.py`; keep the field/label/link differs + `compute_outbound_mutations` orchestrator |
| `_cli/__init__.py` | extract the LLM/workflow command handlers (`_review*`/`_scan_spec`/`_verify_completion`/`_review_plan`/`_workflow*`/`_prompt*` + their `_render_*_text` formatters) to `_cli/_llm_commands.py`; keep the argv router (`_dispatch`/`main`/`build_parser`) + `_reconcile` |
| `__init__.py` | library facade over the cap (also carries the workflow entrypoints `run_workflow`/`get_workflow_status`/`get_workflow_result` + `attach_commits`, epic a88f). KEEP as one surface: it is a deliberate flat public-API namespace whose functions share private helpers; a read/write split forces re-exports for no readability gain |
| `_engine_support/reads.py` | split the CLI `_cmd_*` arms from the `*_state` facades along the existing seam (the facades are imported widely; the `_cmd_*` arms only by the local `main`) |
| `config.py` | split the dataclass/schema from the env/CLI-override + cache machinery along the existing seam |
| `mcp_server.py` | thin FastMCP tool layer. `build_server` is ~30 `@mcp.tool()` closures over a local `mcp`; splitting needs a registrar refactor — watch, don't split preemptively |
| `llm/workflow/lint_refs.py` | grew past cap adding the prompt/step CONTRACT awareness (workflow authoring v2, 5e78): the engine-injected-inputs allow-list + the `${{ steps.*.outputs.* }}` name-existence map. Extract a `lint_contracts.py` once stories e050 (8 op contracts) + c768 (3-state validation depth) add the related logic that clears the 100-LOC floor; today that seam alone is sub-floor |
| `rebar_reconciler/applier.py` | split the apply-dispatch table from the per-action handlers |

`src/rebar/llm/runner.py` was **decomposed** in WS-A (epic a88f): the
filesystem/repo cluster (`_safe_path`, `_git_tracked`, `_discovery_filter`,
`_within_root`, the per-call caps + noise sets) moved to
`src/rebar/llm/fs_tools.py` (the langchain tool-builder that lived there was later
removed in the d6d1 cutover; the shared path-safety helpers remain and are reused by
the pydantic-ai tools in `pai_tools.py`), bringing `runner.py` back under the soft
cap. `fs_tools.py` is also where the workflow engine's git-ref snapshot code (WS-D)
will land.

Files in the 500–800 band (`_commands/transition.py`, `_commands/composer.py`,
`_engine_support/next_batch.py`, `llm/runner.py`, and several `rebar_reconciler/`
modules — `apply_inbound.py`, `_advisory_lock.py`, `acli.py`, `inbound_differ.py`,
`differ.py`, `batch_dispatch.py`, `acli_cli_ops.py`) are at the ceiling, not over
it — watch, don't split preemptively.
