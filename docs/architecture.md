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
  - **Library** (`src/rebar/__init__.py`) — a thin public-API namespace that
    re-exports typed in-process functions from the topical `_lib_*` submodules
    (`_lib_writes` / `_lib_gates` / `_lib_reads` / `_lib_ops`) over
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
    cross-client advisory lock — a single-writer-by-design pass-lock on the
    self-healing `refs/reconciler/*` bare-ref CAS lock (the legacy tickets-branch
    `.reconciler-pass-lock` `file` backend + the `lock_backend` selector were removed
    pre-1.0; epic
    dust-troth-naval / ADR 0031).
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

### Two writers, one store

rebar's git-backed event store has **two independent writers** that must not be
conflated — a recurring confusion for agents scoping bulk-write work:

1. **The local ticket-store write path** — `rebar._store.event_append`
   (`stage_and_commit` / `write_and_push`) plus the inline locked cores in
   `rebar._commands.txn` (transition/claim) and `_commands.delete`. Every CLI,
   library, and MCP mutation — and every bulk operation (import/export/migration) —
   writes through here. Default granularity is **one event = one commit**; a few
   inline cores (claim, delete, `compact-all`) already stage several event files
   into a single commit.
2. **The Jira reconciler** — `rebar_reconciler/` (shipped as `_engine/` package
   data) — a **bridge** that syncs the local store ↔ Jira. It is a *client* of the
   store, not the store itself.

**What they share:** only the low-level single-writer lock (`rebar._store.lock`,
invariant I5) and the canonical event-byte contract (`rebar._store.canonical`).
They do **not** share a write API.

**The trap (read this before scoping any "batch write" work).** An agent scoping a
*local* batch-write greps for "batch"/"commit" and lands in the reconciler — the
wrong system. Two specific false friends there:

- `applier._apply_batch` is an **outbound Jira REST** mutation sequencer (it batches
  *Jira API calls*), **not** a local git-commit batcher.
- The inbound path writes local events via `inbound_translate._write_event_file` —
  one event file per call, under the store lock, via `os.replace`, with **no**
  `git add`/`commit` of its own (it does *not* go through `stage_and_commit`). Those
  files are committed by the reconciler pass's own orchestration, not the local
  write path.

Both are **Jira-sync internals.** If you are reducing commit flood on **local** bulk
writes (import/export/migration), the batch-write primitive belongs in
`rebar._store`; do **not** route local writes through, or "extract" a shared
primitive out of, the reconciler.

**Overloaded vocabulary.** The same words mean different things inside vs. outside
the reconciler: *reconcile* = the local↔Jira bridge pass; *apply* (in the
reconciler) = applying *inbound Jira changes* as local events; *batch* (in the
reconciler, `_apply_batch`) = **outbound Jira REST** call batching, distinct from a
local store commit-batch; *sync* = push/pull of the `tickets` branch
(`rebar._store.push`/`sync`), distinct from Jira sync.

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

- **Attaching to an existing `origin/tickets` (non-interactive bootstrap).** A third
  case behaves like symlinking, not first-time init: when a `tickets` branch already
  exists **locally or on `origin`**, materializing the tracker only *mounts* that
  pre-existing shared state (a linked worktree via
  `rebar._commands.init._mount_or_create_branch`'s local/remote arms) — it fabricates
  no new orphan history. So the auto-init gate does it **automatically, without a
  prompt, even with no TTY** (discriminator
  `rebar._commands.init.pending_init_attaches_to_existing`). This is what makes rebar
  usable out-of-the-box for **CI / agent / headless environments**: a fresh clone
  whose remote already carries `tickets` runs `rebar search`/`show`/etc. with no
  interactive terminal and no manual `git worktree add` + `.env-id` seeding. Only a
  *genuine* first-time init (no local or remote `tickets` branch to attach to) still
  requires consent, since that one mutates the host repo.

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
| `rebar_reconciler/inbound_differ.py` | crossed the cap (was exactly at 800) adding the `idea ↔ IDEA` status-map entry (epic idea-status). Split along the existing seam: the hand-maintained `_JIRA_TO_LOCAL_STATUS` map + status-translation helpers (`_jira_status_to_local` and friends) into a sibling `inbound_status.py`, leaving the field/comment/link diffing in `inbound_differ.py` — both stay well above the 100-LOC floor |
| `rebar_reconciler/reconcile.py` | The differ-running phase extraction has **shipped** (`run_differs.py`, ~640 LOC), but `reconcile.py` is still over cap (~1070 LOC). Next seam: extract the `_PassContext`-driven pass-phase helpers that cluster around `reconcile_once` — `_load_snapshots`, `_handle_corrupt_snapshot`, `_apply_mutations`, `_persist_and_log` — into a sibling `reconcile_passes.py`, leaving `reconcile_once` as the thin sequencer |
| `_commands/fsck.py` | was exactly at the 800 cap; crossed it (→816) adding story 21dd's store-compat wiring (the `_repair_ticket` fail-closed `except` + the read-only diagnostic's structured `compat_error` surfacing in `fsck_cli`). Split along the existing seam: extract the repair cluster (`_repair_plan`, `_repair_ticket`, `_repair_run`, the schedule-disable/enable helpers) into a sibling `fsck_repair.py`, leaving the diagnostic scan + `_transform_json` in `fsck.py` — both stay well above the 100-LOC floor. Tracked as a follow-up (discovered_from 21dd) |
| `config.py` | split the dataclass/schema from the env/CLI-override + cache machinery along the existing seam |
| `llm/workflow/lint_refs.py` | grew past cap adding the prompt/step CONTRACT awareness (workflow authoring v2, 5e78): the engine-injected-inputs allow-list + the `${{ steps.*.outputs.* }}` name-existence map. Extract a `lint_contracts.py` once stories e050 (8 op contracts) + c768 (3-state validation depth) add the related logic that clears the 100-LOC floor; today that seam alone is sub-floor |
| `llm/plan_review/attest.py` | the fastest-growing file in the tree (kind-keyed attestations, epic dark-acme-lumen, + the completion-aware `delivered_now` predicate). Two candidate split seams: the completion-delivery cluster (`_attested_delivered` / `_supersedes_child`) into `attest_delivered.py`, and the validity-computation cluster (`compute_validity` + the reopen/code-drift/material-edit invalidation checks) into `attest_validity.py`. Note the kind-generic validity/signing surface is gate-neutral (the completion gate imports it too), so a gate-neutral home is preferable to keeping it under `plan_review/`. Ceiling nudged (+6, to 861) for the surfaced-only audit annotation (bug old-frilly-plankton) |
| `llm/plan_review/det_floor.py` | crossed the cap adding the verify-command lint (epic cite-stone-sea / WS4, G-3a). Extract the lint cluster (`_lint_verify_command`, `_verify_command_strings`, the `_VERIFY_*` / `_GREP_*` pattern constants) into a sibling `det_verify_lint.py` — the seam already exists (P1–P9 call it, don't share its internals). Ceiling nudged (957→975, epic 9d50 calibration) for a8e5's DET-tier hygiene backstop (`det_finding_has_subject` + the two emission-point filters) |
| `llm/plan_review/passes.py` | crossed the cap across epic cite-stone-sea (WS7 shared preamble, WS8 the foundation/enhancement MOVE_REGISTRY entry, WS9 cohort stamping). Extract the coach cluster (`MOVE_REGISTRY`, `load_move_registry`, `pass4_coach`) into a sibling `coach_moves.py` along the Pass-4 boundary the module already carves. Ceiling nudged (852→871, epic 9d50 calibration) for d4cf's G7 parent-realign coach move + a8e5's operator-attested coach move |
| `llm/workflow/gate_dispatch.py` | crossed the cap adding the code-review finding-memory finalization (epic super-path-bag): the deps computation seam, the region-gated novelty floor invocation, and the local session-artifact resolve/create/link helpers. Ceiling pinned to the stack's landing size (910), mirroring the `reconcile.py` in-flight-stack precedent. Next seam: extract the code-review post-verdict finalization (deps + region floor + session-artifact emit, incl. `_resolve_or_create_session_artifact` / `_link_session_artifact`) out of the generic dispatcher into a `code_review/`-local `finalize.py`, leaving `_run_code_review_gate` a thin sequencer |
| `llm/runner.py` | crossed the cap across epic jira-reb-687 (LLM failure-mode handling): the shared runner seam absorbed transport-retry model construction (`_build_retrying_anthropic_model`, arcticduck), per-request/per-tool timeout wiring (hoopoe), the silent-success NativeOutput stop-reason check + bounded faulty-output reask (drake), and the agent-build capability check + usage-plausibility warning (anole). Ceiling pinned to the stack's landing size (850), mirroring the `gate_dispatch.py` in-flight-stack precedent. Next seam: extract the anthropic-model construction cluster (`_build_retrying_anthropic_model`, `_local_proxy_bypass_base_url`, the httpx timeout/transport wiring) into a sibling `anthropic_model.py`, leaving `PydanticAIRunner.run` a thin agent-build sequencer |

| `rebar_reconciler/outbound_differ.py` | crossed the cap (862) adding the identity-mapping assignee accountId fast-path (epic gnu-whale-ichor / 264f): the 3-tuple `_assignee_resolver`, `_resolve_assignee_account_id`, and the transient `/user/search` bootstrap. Next seam: extract the assignee-resolution cluster (`_assignee_resolver`, `_resolve_assignee_account_id`, `_USER_SEARCH_METHODS`) into a sibling `outbound_assignee.py`, leaving the field diffing in `outbound_differ.py` |
| `rebar_reconciler/dispatch_one.py` | crossed the cap (886) adding the reporter-by-accountId dedicated REST sub-call + the assignee accountId sentinel plumbing (264f): `_update_one_apply_reporter`, the assignee-flag pop, and the lazy `alert_store` degradation helpers. Next seam: extract the pop-before-filter apply phases (`_update_one_apply_parent`, `_update_one_apply_reporter`, the sentinel pop) into a sibling `dispatch_apply_phases.py`, leaving `update_one` a thin sequencer |

`src/rebar/__init__.py` was **split** along its concern seams (ticket S3 / 4532),
**reversing** the earlier "KEEP as one surface" decision: the ~50 public wrapper
bodies moved into four topical `_lib_*` submodules, leaving `__init__.py` a thin
public-API namespace that re-exports them. The split: `_lib_writes.py` (ticket
lifecycle + mutations + signing — also home to the private `_python_leaf` leaf-write
adapter), `_lib_gates.py` (the quality gates, file-impact / verify-command get&set
pairs, `grounding_info`, `summary`), `_lib_reads.py` (queries, NDJSON export/import,
`fsck` — also home to the private `_json_or` helper), and `_lib_ops.py` (the
workflow-engine entrypoints `run_workflow`/`get_workflow_status`/`get_workflow_result`,
the Jira `reconcile` launcher, and the `bridge_fsck` audit). `__init__.py` re-exports
every name (with its identical signature and `__all__`), so `import rebar` /
`from rebar import …` / `rebar.<name>` — including `rebar._python_leaf` and
`rebar._json_or` — are unchanged. `_lib_gates` imports `_python_leaf` one-way from
`_lib_writes` (no cycle). This brought `__init__.py` back under the soft cap (dropped
from the allowlist), and every new module sits comfortably within the 100–800-LOC band.

`src/rebar/llm/runner.py` was **decomposed** in WS-A (epic a88f): the
filesystem/repo cluster (`_safe_path`, `_git_tracked`, `_discovery_filter`,
`_within_root`, the per-call caps + noise sets) moved to
`src/rebar/llm/fs_tools.py` (the langchain tool-builder that lived there was later
removed in the d6d1 cutover; the shared path-safety helpers remain and are reused by
the pydantic-ai tools in `pai_tools.py`), bringing `runner.py` back under the soft
cap. `fs_tools.py` is also where the workflow engine's git-ref snapshot code (WS-D)
will land.

`src/rebar/llm/prompting/prompts.py` was **split** along its front-matter seam (epic 5ca8 /
`dazed-daisy-bur`): the front-matter I/O cluster (`parse_front_matter`,
`_split_front_matter_raw`, `write_front_matter`, `_refuse_newer_schema_version`, the
`_FRONT_MATTER` fence + `FRONT_MATTER_KEYS`/`PROMPT_SCHEMA_VERSION`, and the
`PromptError`/`PromptVersionError` exceptions — moved together because
`PromptVersionError` subclasses `PromptError`) moved to
`src/rebar/llm/prompting/prompts_frontmatter.py`, bringing `prompts.py` back under the soft cap.
`prompts.py` re-exports every moved name, so `from rebar.llm.prompting.prompts import …`
call-sites (and `rebar.llm.prompting.prompts.<name>` attribute access) are unchanged. The
cache-split helpers (`split_volatile`/`strip_volatile_marker`/`resolve_prompt_cached`)
stay in `prompts.py` (they call `resolve_prompt`).

`src/rebar/_engine/rebar_reconciler/applier.py` was **split** along its
dispatch/handlers seam (epic 5ca8 / `self-waltz-ace`): the per-action batch
orchestration that wraps `batch_dispatch`'s `create_one`/`update_one`/`delete_one`
(REST-budget counting on create; the 404 / assignee soft-fails, sub-op telemetry,
the silent-no-op canary, and set-field provenance on update) moved to a sibling
`apply_handlers.py` as `handle_create`/`handle_update`/`handle_delete`/`handle_unknown`
behind a `dispatch_mutation` table over a `BatchApplyContext`. `applier._apply_batch`
is now a thin sequencer — resolve transport → cross-project guard → HEAD-drift
recheck loop → per-mutation dispatch + record → manifest-write tail — whose
per-mutation step is the extracted `_recheck_drift` + `_apply_one` helpers (nesting
depth ≤ 4). The rebar-id label-write audit guard and the `_load_acli`/`_load_concurrency`
seams stay resident in `applier` (the test suite patches them there);
`apply_handlers` imports only downward (`batch_dispatch`/`pass_io`), so `applier`
imports the handlers back without a cycle. This brought `applier.py` back under the
soft cap (dropped from the allowlist).

`src/rebar/_engine/rebar_reconciler/outbound_differ.py` was **split** along its
three differ seams (epic 5ca8 / `unfed-liner-arson`): the comment-diff cluster
(`_diff_comments` + `_normalize_comment_body`/`_decorate_outbound_comment`/
`_map_comments_for_create`/`_is_machine_marker_comment`) moved to
`outbound_comments.py`; the field-mapping + field-diff cluster
(`_map_local_to_jira_fields`/`_extract_jira_field`/`_assignee_matches`/
`_local_matches_prev`/`_parent_clear_is_managed`/`_diff_fields` + the
`_LOCAL_TO_JIRA_*` maps) to `outbound_fields.py`; and the link-diff cluster
(`_existing_jira_links`/`_diff_links`/`_diff_link_removals` + the relation maps)
to `outbound_links.py`. `outbound_differ.py` keeps the `compute_outbound_mutations`
orchestrator + the label/status-annotation differs, and re-exports the moved
names so `outbound_differ.<name>` keeps resolving for the test suite; each sibling
imports one-way (its own lazy `_load_adf`/`_load_comment_limits`) so there is no
import cycle. The orchestrator's nine positional params collapsed into an
`OutboundDiffConfig` dataclass and the mutable `absent_alive_fields` out-param
became the second element of its `(mutations, absent_alive_fields)` return tuple.
This brought `outbound_differ.py` back under the soft cap (dropped from the allowlist).

`src/rebar/_cli/__init__.py` was **split** along its command-handler seam: the
LLM/agent-operation handlers moved to `src/rebar/_cli/_llm_commands.py` and the
workflow handlers to `src/rebar/_cli/_workflow_commands.py`, leaving the argv router
(`_dispatch`/`main` + `_reconcile`) under the soft cap. `main()`
imports the entrypoints it dispatches to; the two command modules don't import each
other.

`src/rebar/_engine_support/reads.py` was **split** the same way: the argv-facing
`_cmd_*` arms + the `main` dispatcher moved to
`src/rebar/_engine_support/reads_cli.py`, leaving the widely-imported `*_state`
facades (and `tracker_dir`/`ensure_fresh`/`ReadError`) in `reads.py` under the cap.
`reads_cli` imports the facades from `reads` (one direction); `reads.main` is a thin
lazy wrapper that delegates to `reads_cli.main` for backward compatibility (avoids an
import cycle).

Files in the 500–800 band (`_commands/transition.py`, `_commands/composer.py`,
`_engine_support/next_batch.py`, `llm/runner.py`, and several `rebar_reconciler/`
modules — `apply_inbound.py`, `_advisory_lock.py`, `acli.py`, `inbound_differ.py`,
`differ.py`, `batch_dispatch.py`, `acli_cli_ops.py`) are at the ceiling, not over
it — watch, don't split preemptively.

## mypy strictness ratchet

`make typecheck` (`mypy src/rebar`) gates the whole library. Two ratchet dials in
`[tool.mypy]` tighten it over time, mirroring the module-size allowlist's *shrink-only*
discipline:

- **`check_untyped_defs = true`** (global) — mypy checks the *bodies* of un-annotated
  functions, not just their signatures, so bugs inside un-typed defs can't slip through.
- **`disallow_untyped_defs`** via `[[tool.mypy.overrides]]` — enabled per-package for
  packages whose functions are fully annotated. This set is **shrink-only for the exempt
  list**: a package may only be **added** to the strict override (never removed).
  `tests/unit/test_mypy_ratchet.py` pins the committed baseline (`rebar.graph`,
  `rebar.grounding`) as a subset of the enabled set, so a regression turns the build red.

**To promote a package** into the strict set: annotate its remaining defs until
`mypy src/rebar/<pkg> --disallow-untyped-defs` is clean, then add `rebar.<pkg>.*` to the
override `module` list. New `type: ignore` must carry a specific code (e.g.
`type: ignore[arg-type]`); blanket `ignore_errors` is not used.
