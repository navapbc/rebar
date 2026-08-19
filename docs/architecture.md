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
    leave it off so logs stay discoverable (see event-schema.md "The session_log ticket type").
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
    dust-troth-naval / ADR 0031). Canonical `bridge preview` / `bridge sync` and
    retained reconcile adapters normalize before this shared spine; see
    [ADR 0092](adr/0092-bridge-primary-vocabulary-compatibility-adapters.md).
  - **Import/export** (`rebar._io`, code at `src/rebar/_io/`) — NDJSON
    export/import of ticket state backing `rebar.export_tickets`/`import_tickets`,
    the CLI `export`/`import` subcommands, and the MCP equivalents (see
    [import-export.md](import-export.md)). Export streams replay-derived states;
    import is idempotent (re-importing the same stream is a no-op).

- **LLM agent operations** (`rebar.llm`, code at `src/rebar/llm/`) — an OPTIONAL
  framework for tool-using LLM agents that emit structured findings, exposed over
  library/CLI (`rebar review-plan`)/MCP (`review_plan`). The engine core needs NO LLM
  dependency (its only runtime deps are `pyyaml`, the workflow DSL loader, and
  `jsonschema`, the schema-registry/contract validator); everything
  here is behind the `nava-rebar[agents]` extra and lazy-imported. A pluggable `Runner`
  (the in-process, provider-agnostic pydantic-ai runtime; a `FakeRunner` for tests)
  runs the agent with read-only repo file tools + MCP
  tools; output is constrained to the `review_result` JSON Schema.
  Langfuse is the optional OTLP tracing endpoint (`[tracing]` extra); reviewer prompts are
  git-canonical (packaged `reviewers/*.md` or project `.rebar/prompts/`). See
  [llm-framework.md](llm-framework.md).

### Accepted configuration and provider-composition boundary (RP-04; migration pending)

[ADR 0098](adr/0098-operation-scoped-config-and-provider-composition.md) establishes one
immutable, serializable, non-secret snapshot per operation, composed at the application
boundary with separate live `GitRuntime`, `LLMRuntime`, and selected `BridgeRuntime`
bindings. Provider SDKs/helpers remain responsible for credential issuance, storage,
refresh, and invalidation; Rebar-owned factories retain Rebar's retry, timeout, cache,
output, error, and lifecycle policy. Static material is provider-specific and unwrapped
only at the sending adapter. The migration is not yet implemented; current
ambient/per-consumer configuration paths remain until RP-04's expand/contract cutover.

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

**The field-symmetry registration contract.** Every reconciled field must declare
its symmetry class in `rebar_reconciler/_field_contract.py` — `bidirectional`
(value codec round-trips on its canonical preimages; declared lossy edges only),
`add_wins` (a value added and removed in the same pass stays added — the reducer's
intra-event TAG_DELTA contract), or `one_way_gated` (removals propagate to the peer
only through the `should_propagate_removal` managed-ref gate). The registry is
declarative — call sites keep their existing mechanisms (`config` status maps,
`link_direction`, `conflict_resolver.FIELD_CLASSES`, the managed-ref gate) — and
`tests/unit/rebar_reconciler/test_field_contract_properties.py` enforces it: a
field handled by the conflict resolver or the differ's name-canonicalization map
without a registry entry fails test collection, and each declared class is
asserted against the real code path. Adding a reconciled field means declaring its
symmetry here first.

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
in the Jira vendor-adapter sub-package (ADR 0035 §(c)) at
`rebar_reconciler/adapters/jira/acli.py`, reached via ordinary
`from rebar_reconciler.adapters.jira import acli` package imports.

#### Which tracker the reconciler drives — `reconciler.backend`

The reconciler is not Jira-Cloud-only. `[tool.rebar.reconciler].backend` selects the vendor
adapter through the in-tree registry (`rebar_reconciler._backend_registry.select_backend`,
ADR 0035 §(d)); an unknown key raises `BackendRegistryError` naming the registered keys.
Two keys exist today:

| `reconciler.backend` | Adapter package | Transport | Extra needed |
|---|---|---|---|
| `"jira"` (default) | `adapters/jira/` | ACLI subprocess + REST v3 (ADF bodies, `accountId` identity) | none |
| `"jira-datacenter"` | `adapters/jira_datacenter/` | `pycontribs/jira` over REST v2 (plain/wiki bodies, `name` identity), PAT bearer auth | **`nava-rebar[jira-datacenter]`** |

The two share one **Jira-family layer**, `adapters/jira_family/` — the value maps, link-relation
vocabulary, field sanitizers, `rebar-id:` identity convention, and probe classifier exist once
and are parameterized by two contracts, `RichTextCodec` and `UserIdentityModel`. The dependency
direction is one-way (`jira_family/` imports neither concrete backend) and is machine-checked by
`tests/unit/rebar_reconciler/test_jira_family_boundary_heldout.py`. See
[ADR 0055](adr/0055-jira-family-sub-seam.md).

The Data Center Markdown-to-wiki renderer implements ADR 0096's two-cadence Pandoc boundary.
Gerrit Verify runs the complete segmentation, protection, settling, richness, alignment, and
exact-byte assertion classes against deterministic per-stratum outputs under
`tests/fixtures/dc_wiki_replay/`, plus one real product-path body from each corpus stratum and
the compact pin/provenance probes. The weekly/manual External Integration Tests workflow owns
complete replay of every Pandoc-bound corpus unit and required settling pass on both Linux and
macOS through its `pandoc-corpus-replay` matrix job. Version or platform-binary fingerprint
drift fails before committed outputs are trusted. Regenerate deliberately with
`python scripts/generate_dc_wiki_legacy_outputs.py`; validate without writing with `--check`.

The `[jira-datacenter]` extra declares `jira>=3.8,<4` (pycontribs/jira) and is the reconciler's
only third-party runtime dependency. It is imported **lazily**, inside the functions that need
it, so `import rebar` and a default install stay dependency-free — enforced by the absent-module
check in `.github/workflows/_optionality.yml`. Operator setup is in
[user-guide.md](user-guide.md#jira) / [jira-sync-setup.md](jira-sync-setup.md).

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
  [`docs/config.md`](config.md) (and the generated key-by-key
  [`docs/config-reference.md`](config-reference.md); secrets handling in
  [`docs/security.md`](security.md)).

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
`AGENTS.md`.

## Sanctioned git-write seams

All git mutations of the tickets store flow through a small set of write seams:
`_store/gitutil.run_git_write` (and the locked-store internals in
`_store/event_append.py`, `_store/txn.py`, `_store/push.py`, `_store/sync.py`),
the reconciler's `git_adapter.py` / `_ref_lock.py`, and the store-maintenance
commands (`init`, `compact`, `delete`, `fsck*`). CI enforces this with the
**raw-git-write gate** (`scripts/check_raw_git_writes.py`, ticket d37e): a raw
`subprocess` git mutation or a mutation-verb call through a wrapper name outside
a seam fails the build, as does a workflow step running `git add/commit/push`
inside a `.tickets-tracker` context. Legitimate sites carry a reasoned
`# raw-git-ok: <reason>` marker (empty reasons are rejected); sandbox/eval repos
and generic command runners are marked, not exempted. This is a different
marker from `# tickets-boundary-ok`: that convention sanctions
boundary-crossing *reads/layout* knowledge of `.tickets-tracker`, while
`# raw-git-ok` sanctions raw git *writes*.

## Module-size policy

rebar is built to be edited by agents, which read a unit whole. The balance is
between *editability* (a file an agent can load and reason about in one pass) and
*fragmentation* (so many tiny files that following a change means chasing imports).
The policy:

- **Target 200–500 LOC** per unit; a unit is one cohesive responsibility.
- **Hard cap 800 LOC — absolute.** Over 800 is a smell to address — and only by a
  *real* split, never a mechanical one. There is **no allowlist escape hatch**: epic
  716f split the last grandfathered over-cap module and removed the allowlist +
  ceilings ratchet, so the cap now admits no exemptions.
- **Split only along call-graph seams that already exist** — extract a cluster of
  functions that already call each other and little else. Do not split a unit just
  to hit a line count.
- **Never create files < 100 LOC by splitting.** Two 60-line files that always
  change together are worse than one 120-line file.

### Enforcement

A CI **module-size gate** — in both `.github/workflows/gerrit-verify.yaml` (gating the
`Verified` vote) and `.github/workflows/test.yml` (the post-merge mirror) — **fails the
build** when ANY `src/rebar/**/*.py` file exceeds the cap. No allowlist, no exemptions;
a new over-cap file must be split. The limit is single-sourced in
**`.github/module-size-limit.txt`** (read by the gate and by
`tests/unit/test_module_size_contract.py`, so they never disagree) and is **locked**: the
gate compares the change's limit against `main`'s and fails any change to it, so raising
or lowering the 800 value requires an **administrator** to override the gate
(force-submit) — a normal contributor cannot change the limit through the review process.

The gate itself lives in exactly one place — the reusable
`.github/workflows/_build-and-test.yml` step that both `gerrit-verify.yaml` and `test.yml`
*call* — so its rule has a single definition. Alongside the hard-cap failure, that same step
runs a **non-blocking near-cap leading indicator**: it emits a `::warning::` for every
`src/rebar/**/*.py` file within 10% of the cap (>= `LIMIT - LIMIT/10` LOC, e.g. >= 720 at the
800 cap) and always exits 0. The band is the same 10% `code_health.size_near_fraction` (default
0.1) uses to compute `module_size_distribution.near_cap_count`, and the pass reuses the gate's
own `find | wc -l` rather than defining the rule twice. The hard cap tells you *after* a file is
already over; this surfaces the next split *before* a contributor is blocked by it.


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

`src/rebar/llm/plan_review/pass1.py` and `src/rebar/_store/push.py` were each **split**
along an existing call-graph seam (ticket 5e53, the `broke-boarish-hagfish` recurrence
epic) to clear the hard cap. `pass1.py`'s container (G3/G4) pairing stage — `_run_container`
and its single-caller helpers — moved to `llm/plan_review/container_stage.py`, with the
shared `_submit_ctx` relocated to `generation.py` so the dependency stays one-way
(`pass1 → container_stage → generation`); `orchestrator.py` / `provider_parity.py` keep
importing the moved names via re-export. `push.py`'s stash/dirty-tree and
non-fast-forward recovery cluster moved to `_store/push_recovery.py`: because `push._git`
is monkeypatched across ~25 test sites, each moved function takes the calling module as a
`core` parameter and resolves `core._git`/`core.logger` at call time (the
`_ref_lock_push.py` late-binding pattern), and `push.py` retains old-signature delegating
shims plus re-exports so `push.<symbol>` attribute access (and
`_resolve_conflicted_apply.__doc__`) survives the move. Both new modules sit within the
100–550-LOC band.

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
- **`disallow_untyped_defs`** (ratchet **tier 1**) via `[[tool.mypy.overrides]]` — enabled
  per-package for packages whose functions are fully annotated. This set is **shrink-only
  for the exempt list**: a package may only be **added** to the strict override (never
  removed). `tests/unit/test_mypy_ratchet.py` pins the committed baseline as a subset of
  the enabled set, so a regression turns the build red.
- **`disallow_any_explicit`** (ratchet **tier 2**) via a second
  `[[tool.mypy.overrides]]` block — enabled per-package for packages that carry **no
  explicit `Any` at all**. Same shrink-only discipline, pinned by the same test, which
  also asserts tier 2 ⊆ tier 1 (banning `Any` is a *strengthening* of "everything is
  annotated", never a substitute for it).

**To promote a package** into tier 1: annotate its remaining defs until
`mypy src/rebar/<pkg> --disallow-untyped-defs` is clean, then add `rebar.<pkg>.*` to the
override `module` list. New `type: ignore` must carry a specific code (e.g.
`type: ignore[arg-type]`); blanket `ignore_errors` is not used. **To promote into tier
2**, additionally replace its explicit `Any`s with real types until
`mypy src/rebar/<pkg> --disallow-untyped-defs --disallow-any-explicit` is clean.
`cast()` is **not** a promotion tool: it silences the checker at the same place `Any`
did, so it buys neither driver below.

### Why two tiers — measured

Tier 1 is necessary but **not sufficient**. `rebar.grounding` was promoted with zero
un-annotated defs and, at the same time, 116 explicit-`Any` sites; `rebar.metrics` and
`rebar.audit` are in the same shape. A package can therefore satisfy tier 1 while
remaining opaque to *both* consumers the ratchet exists for:

1. **Navigability.** Pyright/Serena resolve references only through typed receivers. An
   `Any` receiver makes `find_referencing_symbols` return a **false empty** — not an
   error — so a symbol with real call sites reads as unused
   ([rebar:dbf9-3fc8-625d-4730]; see also `docs/code-navigation.md`).
2. **Contracts, hence testability.** A port typed `Any` cannot be enforced, so a
   component can silently fail to satisfy the interface it claims
   ([rebar:cc77-8120-bcc6-47e8], root-caused defect [rebar:a357-b747-ece9-4cf5]).

Measured baseline when tier 2 was introduced (`mypy src/rebar/<pkg>` with each flag):

| package | un-annotated defs | +explicit `Any` | tier |
|---|---|---|---|
| `_cli`, `_snapshot` | 0 | 0 | 1 + 2 |
| `_engine_support`, `_io`, `_store`, `attest`, `audit`, `graph`, `grounding`, `metrics`, `opcert_service`, `review_bot`, `schemas` | 0 | 5–116 | 1 |
| `reducer` | 1 | 5 | — |
| `_engine` | 134 | large | — |
| `_commands` | 146 | large | — |
| `llm` | 293 | large | — |

Both tiers shrink from there. Not every `Any` is illegitimate — a genuine dynamic
boundary (JSON payloads, `spec_from_file_location` loaders, third-party stubs) is a
deliberate `Any`, and the tier-2 set is grown package-by-package rather than by a global
flag precisely so those stay visible instead of being papered over with casts.
`reducer` is the smallest remaining example: its single un-annotated parameter,
`reducer._filters.match_predicate(value)`, is a query-predicate value whose type depends
on the operator, so it needs a typed predicate value — not a rubber-stamp annotation.

### The ratchet does not protect a port boundary — two ways to pass it blind

`disallow_untyped_defs` requires *an* annotation, never a *useful* one. At a port
boundary — a parameter carrying `TicketTransport` or another Protocol — there are two
distinct spellings that satisfy the ratchet while leaving the parameter exactly as
unchecked as no annotation at all. Both were measured (ticket cc77); both are why
promoting a package must not be treated as evidence its port boundaries are typed:

1. **`client: Any`.** Satisfies `disallow_untyped_defs`. A call to any member —
   declared or not — is accepted. Promoting a package by spraying `Any` closes the
   ratchet ticket and changes nothing.
2. **A port annotation imported through `rebar_reconciler.`.** `rebar_reconciler` is not
   an importable distribution — it is a top-level name created at runtime by injecting
   `src/rebar/_engine` onto `sys.path`. mypy is never told about that injection, and
   `ignore_missing_imports = true` is set above, so `from rebar_reconciler._backend import
   TicketTransport` resolves to `Any`. `reveal_type` on such a parameter prints `Any`,
   and a call to a bogus member produces no error. The **relative** form
   (`from ._backend import TicketTransport`) resolves to the real type and restores
   attribute checking against the same class, metaclass and all.

Route 2 is the dangerous one: the diff shows a correct-looking port annotation, and
`make typecheck` stays green because it was never going to say anything. This is the
enabling condition behind [rebar:a357-b747-ece9-4cf5], where a writing reconcile pass
crashed on `set_entity_property` — a method the core calls from three sites and the port
never declared. `tests/unit/rebar_reconciler/test_transport_param_typing_cc77.py` fails
on all three spellings (bare, `Any`, shim-path) for the core reconciler's
`client`/`transport` parameters, so a later "annotate it to pass the ratchet" change
cannot reintroduce the blindness.
