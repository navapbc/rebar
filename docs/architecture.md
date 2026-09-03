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
    write tools gated by `REBAR_MCP_READONLY`; bridge preview is non-mutating.
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

### Implemented operation-scoped configuration and provider composition boundary

[ADR 0098](adr/0098-operation-scoped-config-and-provider-composition.md) established the boundary implemented by RP-04 under ticket `vibrant-legal-hind`. `rebar.config.compose_operation_snapshot` resolves one immutable, serializable, non-secret `OperationSnapshot` for an operation. The snapshot contains effective values, source provenance, the repository root, and an envelope version. It excludes credentials and capability objects.

Behavior-bearing runtime bindings remain outside the snapshot. `ReconcilerRuntime` derives captured Jira scope and the selected backend from an operation snapshot. `LLMRuntime` carries provider-native `AnthropicAuth`, `BedrockAuth`, and `OpenAIAuth` values into provider construction. The review bot stores that runtime beside non-secret startup policy in `StartupBinding`. The operation certificate service composes `OpcertSigner` at startup and passes it through the context-local `bound_signer` seam. ADR 0098 uses `GitRuntime` and `BridgeRuntime` as design-role labels. The code does not expose classes with those names.

`OperationSnapshot` is now AUTHORITATIVE (not merely diagnostic) for the CLI, MCP read/write, and Python command/store surfaces (RP-04 S2, ticket `luminous-stenchful-cottontail`): `compose_and_bind_operation_snapshot` composes ONE snapshot per operation, binds it active via a contextvar, and `rebar.config`'s `tracker_dir`/`tickets_branch`/`tickets_remote` consult that bound snapshot (falling back to ambient `load_config` only when none is bound), so a later environment/config/CWD mutation cannot change an in-flight operation's tracker location. Push mode (`config.resolve_push_mode`) is a deliberate, tested exception that stays live per-call rather than frozen, because `_io/import_ndjson.py` toggles it mid-bulk-import. The reusable interfaces and binding details are in [reuse-surface.md](reuse-surface.md#6-operation-scoped-configuration).

ADR 0098 is now COMPLETE for the LLM/gate/workflow surface too (RP-04 S3, ticket `gaudy-working-mantaray`): `rebar.llm.config_binding.compose_and_bind_llm_config` composes ONE `LLMConfig` per public LLM operation — `review_code`/`verify_completion`/`review_plan`/`scan_epics_for_spec` (each reached identically by the CLI, the matching MCP tool, and the Python API) plus `run_workflow` (bound inside `workflow/runs.py::run()`) — and binds it active via the same `gate_config` contextvar mechanism `resolve_gate_config` already used for a bound gate run, so nested subcalls and multi-step workflow steps observe the identical `LLMConfig` instance instead of each independently re-reading the environment. Below-boundary call sites (`enrich`, `epic_bug_screen`, the `overlap/*` workers, `plan_review/attest.py`, the completion/plan-review agent-step bridge) now call `resolve_gate_config(repo_root)` rather than `LLMConfig.from_env()` directly. Unlike the general operation snapshot's fail-open swallow, `compose_and_bind_llm_config` does NOT catch a composition failure — a missing/conflicting decision-bearing provider or credential fails before any external call, with no anonymous/cross-provider fallback (AC3). `redacted_snapshot_values`/`llm_config_fingerprint` give the LLM config the same non-secret, JSON-primitive-only projection `OperationSnapshot` gives the general operation config, via an explicit field allowlist that never exposes `api_key` or the live `ticket_view`. `resign_plan_review` is a documented exception: it makes no LLM call, so it is never bound via the LLM composer (it still gets the general operation-snapshot binding like any other MCP tool). The diagnostic shadow (`emit_shadow_snapshot`/`_shadow`/`shadow_enabled`/`REBAR_OPERATION_SNAPSHOT_SHADOW`) that both S2 and this S3 cutover depended on for a side-by-side parity check is deleted — there is no remaining production caller and nothing left to configure. `scripts/check_config_ownership.py`'s legacy exception list remains empty.

Configuration reads are routed through the approved composition and credential boundaries in `rebar._config_sources` and `rebar._config_resolvers`. `scripts/check_config_ownership.py`, which runs through `make lint`, rejects prohibited ambient reads below those boundaries. Its legacy exception list is empty.

### Implemented lazy CLI command and capability registry (RP-05)

[ADR 0100](adr/0100-cli-command-and-capability-registry.md) records the route, grammar, help, and capability decisions introduced by RP-05. Epic `grapy-cynical-copepod` records the completed implementation, and task `pettish-snippy-rasbora` records the final runtime cutover.

`rebar._cli._registry.ROUTES` is the immutable authority for recognized top-level command spellings and their execution policy. Each `Route` records its spelling and visibility, lazy handler and parser factory references, bounded invocation adapter, initialization and mount policy, confirmation and output policy, and advertised capability keys. Handler and parser references remain `module.path:attr` strings while the registry is imported. This keeps command handlers and optional packages outside startup. Registry validation rejects duplicate or contradictory entries, malformed references, unsupported adapters or initialization policies, and unknown capability keys without importing handlers.

`rebar._cli.main` serves overview, help, and unknown-command requests from committed package data before operation snapshot composition, configuration materialization, store mounting, handler resolution, or optional imports. Command execution then passes through configuration, mount, and confirmation boundaries before `rebar._cli._execute.execute` selects the route. The executor applies the route initialization policy, imports only the selected handler, and invokes it through the route adapter. Core, bridge, and advanced commands use this path, so there is no separate top-level command dispatch ladder.

Side-effect-free stdlib `argparse` factories define each command grammar. Runtime command parsers and `scripts/gen_cli_help.py` use those factories. The generator walks visible entries in `ROUTES` at build or check time and writes `src/rebar/_cli/help/<command>.txt` plus `overview.txt`. Runtime help serves those committed bytes. `scripts/gen_cli_reference.py` also reads `ROUTES` for the command census, which keeps the external CLI reference aligned with the registered surface.

`rebar._capabilities.CAPABILITIES` is the immutable descriptive registry for semantic optional capabilities. Each entry records a packaging extra, a typed missing posture, a probe module, and an installation hint. Route capability keys are validated against this registry. Availability checks use module discovery without importing the optional package. Error-posture capabilities can fail with a targeted dependency error at their selected execution boundary. Domain components retain ownership of unavailable, abstain, and fallback results. Capability checks occur after the relevant mode, backend, analyzer, renderer, or provider is known, rather than during route selection.

ADR 0100 remains the historical rationale for this boundary, including the canonical, compatibility, hidden, and retired spelling taxonomy. The implementation preserves its stdlib-only startup and pre-operation help invariants.

### Implemented reconciler operation coordination (RP-03)

[ADR 0103](adr/0103-reconciler-operation-coordination.md) records the operation-coordination boundary implemented by epic RP-03 (`unfearful-dejected-verdin`). The outbound mutate path is a one-way dependency chain — **planner → coordinator → adapter** — and each layer depends only on the one below it, never upward:

- **Planner.** The differ/planning layer produces provider-neutral `TicketPlan`s (the desired-state deltas arbitrated by the snapshot, three-way-merge, and echo-suppression contracts of ADRs 0004/0026/0029). It decides *what* the desired state is and emits typed mutations; it holds no retry, fuse, or transport knowledge.
- **Coordinator.** `rebar_reconciler.batch_dispatch.coordinate_and_fuse` (over `rebar_reconciler.coordinator.coordinate`) owns *how* one logical operation is invoked: the single bounded `retry_budget.RetryBudget` (at most three physical invocations and 15s cumulative sleep), the observe-before-replay decision after an ambiguous commit, and the pass fuse that contains same-scope failure. It routes each `(direction, action)` to exactly one typed owner (`route_for`) with no dual-send and no class-name/duck dispatch, and projects results onto the five-bucket `CutoverReport`.
- **Adapter.** The provider adapter (`adapters/jira`, `adapters/jira_datacenter`) performs a single physical, as-atomic-as-the-provider-allows operation and reports a provider-neutral `AtomicSignal`/`OperationOutcome`. It owns venue serialization (Cloud ADF vs DC wiki on the description path; summary is venue-agnostic) but **no retry policy, no fuse, and no cross-operation state**.

**Versioned output.** One logical operation resolves to one `operation_outcome.OperationOutcome` — a frozen, provider-neutral value with a stable `logical_id` across physical invocations, bounded and allowlisted diagnostics (routed through the ADR 0041 sanitization seam), and canonical bytes produced only through the `rebar._store.canonical` seam, so equivalent outcomes serialize byte-identically. Outcomes carry an additive, versioned envelope; the eleven `Disposition` values project onto the five stable pass buckets (`applied, recovered, deferred, failed, skipped`) that sum exactly to the mutation count. Adding a field is additive and never changes existing mutation bytes or the canonical hash.

**Future adapter obligations.** A new provider adapter joining this boundary must: expose each supported mutation as a single typed operation returning a provider-neutral signal (no whole-operation retry loop of its own, no `_best_effort` write-swallowing); support the observe step so an ambiguous commit can be resolved by re-observation rather than blind replay (`ReplaySafety.forbidden` for `commit_unknown`); never delete remote work to undo a partial pass (rollback is code/routing reversion plus re-observation); and leave the retry budget, fuse, and pass tally to the coordinator. These obligations are enforced behaviourally by the route census (`tests/unit/rebar_reconciler/mutate/test_coordinator_route_census.py`) and the portable, credential-free Cloud/DC coordinator suite (`tests/unit/rebar_reconciler/mutate/test_reconciler_coordinator.py`).

### Typed outbound mutation payloads (ADR 0107)

[ADR 0107](adr/0107-reconciler-typed-mutation-payload-contract.md) records the
`Mutation.payload` contract: ten typed, `Mapping`-compatible dataclasses in
`rebar_reconciler/mutation_payloads.py` (one per live `(direction, action)`
combination — `OutboundCreatePayload`, `OutboundUpdatePayload`,
`OutboundDeletePayload`, `OutboundProbePayload`, `OutboundConflictPayload`,
`InboundCreatePayload`, `InboundUpdatePayload`, `InboundCleanLabelPayload`,
`InboundRepairPropertyPayload`, `InboundConflictPayload`), each with an
`as_legacy_dict()`/`from_legacy()` round-trip and its own `__post_init__`
validation (e.g. an update payload must change at least one of
changed_fields/comments/labels/links).

**Producer cutover (epic `eb64` / story `single-vast-roan`).** The outbound
producer (`outbound_pass._run_differs_outbound`) now constructs
`OutboundCreatePayload`/`OutboundUpdatePayload`/`OutboundDeletePayload`
directly from each `OutboundMutation`, rather than assembling a raw dict whose
CREATE shape (top-level-spread fields vs. a nested `"fields"` key) was
ambiguous downstream. `batch_dispatch._mutation_to_batch_dict` reads a typed
payload's own named attributes (`.fields` / `.changed_fields`) directly — no
more runtime `"fields" in payload` sniffing for the production path. The
historical dict-shape heuristic in `_mutation_to_batch_dict` is **retained**,
but only as an explicit, documented fallback for a raw-dict `Mutation.payload`
— a genuinely supported, test-exercised construction path (see
`tests/unit/rebar_reconciler/mutate/test_outbound_update_propagation.py` /
`test_outbound_create_binding.py`), not a production code path (verified by
`tests/unit/rebar_reconciler/mutate/test_payload_shadow_route_census.py`'s
source-text census: only `outbound_pass` and `batch_dispatch` import
`mutation_payloads`; no production module imports the side-effect-free shadow
comparator, `payload_shadow.py`).

`applier.apply()`'s **two call shapes remain**: a single typed `Mutation`
dispatches through `_apply_typed`, and a **list** dispatches through the legacy
batch path (`_apply_batch`) — and that list may itself be either typed
`Mutation`s (the real production shape, always typed after the producer
cutover above) or already-dict-shaped legacy batch entries (a distinct,
directly-tested API convention — see
`tests/unit/rebar_reconciler/mutate/test_apply_list_of_mutations.py` and the
~50 tests across `tests/unit/rebar_reconciler/apply/` that hand-build dict
batches to unit-test `_apply_batch`/`create_one`/`update_one`/`delete_one`
independent of how the input was produced). `apply()` still branches on
`isinstance(m, MutationShape)` per list entry to decide whether to convert via
`_mutation_to_batch_dict` — this is that dual-call-shape API, not residual
production-path ambiguity: 100% of what the reconciler pass itself emits is
now a typed `Mutation` with a typed payload, so `_mutation_to_batch_dict` is
never invoked on an ambiguous shape in production.



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

## The tickets-store boundary gate

The ticket store is **relocatable** — `config.tracker_dir()` resolves it through
the `REBAR_TRACKER_DIR` override and the `tracker.dir` key, where an absolute
value relocates the store outright (EV-3b). Code that instead *composes*
`repo_root / ".tickets-tracker"` silently ignores that configuration and works
against a directory the operator never named.

CI enforces this with the **tickets-boundary gate**
(`scripts/check_tickets_boundary.py`, bug 0514): a string literal naming the
store fails the build when it appears in a path-composing position — a `/` join,
an `os.path.join` argument, a `Path(...)` argument, or a name bound to the bare
dir name. It is AST-based, so docstrings, comments, error text, and argparse help
are excluded structurally; only composition is a violation.

Sanction is `# tickets-boundary-ok: <reason>`, and **the reason is mandatory**.
The bare marker predates the gate and was documented but unenforced, so it had
been applied as a rubber stamp: seven of the thirteen defects the gate was
written to drain carried one. A reasonless marker therefore gets its own
diagnostic rather than being reported as unmarked. Legitimate sanctioned shapes
are the default name inside a resolver, and a path built inside a temp or
snapshot directory the code itself just created — never the configured store.

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

### Current policy: no internal-only compatibility shims

Internal-only compatibility shims are prohibited for future private moves.

[ADR 0111](adr/0111-no-internal-only-compatibility-shims.md) is the governing policy for future
private symbol and private module moves. After a private binding moves, rebar keeps exactly one
canonical binding: source imports, tests, string lookups, dynamic imports, and module-qualified
monkeypatch targets must all migrate atomically, and the old private binding is deleted in the
same slice. Do not add a forwarding wrapper, re-export, or deprecated alias solely to preserve an
old private path.

The valid exceptions are the compatibility-bearing surfaces named in
[api-stability.md](api-stability.md): public `rebar.*` facades, CLI/operator deprecation surfaces,
config-key aliases, MCP wire schemas, JSON output schemas, event readers, and persisted-data
migrations. Those adapters are justified by public/operator/wire/data contracts; internal tests or
private imports are not such a contract.

The split notes below are historical implementation records. Where they mention internal
re-exports or shims retained for a past split, read them as pre-policy history unless the note is
protecting one of the public/wire/data exceptions above. They are not future guidance for private
refactors.


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

It was split a second time (epic 5ca8 / `deft-effortless-greatdane`) along its
**resolver / model** seam, because 789 lines left no room to edit it. `prompts.py` keeps
the RESOLVER — which bytes are a given prompt's bytes (packaged `reviewers/*.md` vs a
project `.rebar/prompts/<id>.md` override), the derived reviewer catalog over them, the
front-matter contract, variant overlays, index regeneration, and reviewer selection: all
of it filesystem- and catalog-bound. The I/O-free half — *what a prompt is* — moved to
`src/rebar/llm/prompting/prompt_model.py`: the `Prompt`/`Reviewer` value types, the
`ReviewerError`/`PromptNotFound` error vocabulary, the closed `EXECUTION_MODES` enum, and
the text grammar a prompt is written in (`_VAR`/`template_variables`/`_render_strict`,
the `<!--base-->` and `<!--volatile-->` markers with `split_volatile`/
`strip_volatile_marker`, `SHARED_STANCE_PREAMBLE`/`shared_plan_prefix`, and
`prompt_content_hash`). Every call-graph edge across that seam points one way — the
resolver constructs these types, raises these errors, and renders through these helpers,
and nothing in `prompt_model` calls back — so the new module is a LEAF with no import
cycle. `prompts.py` re-exports all fifteen moved names, which is what keeps both
`from rebar.llm.prompting.prompts import …` call-sites and the resolver's own late
binding unchanged: the two monkeypatch welds in the test suite (`prompts._catalog_dir`,
`prompts._prompt_file`) sit on the resolver side and were deliberately not moved. The
seam is pinned by `tests/unit/test_prompt_model_module_seam.py`.

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

## Mechanism-delta ratchet

Ticket `unblacked-loveless-toad` (9ca8-675e-4dfb-427d). A sample of merged fixes found that
**56% ADD a mechanism** — a lock, a knob, an env var, a gate script, an autouse fixture, a
test helper, a feature flag — against **30%** that are pure logic fixes. Every such fix leaves
the repository with more surface than it found, and that surface is exactly what produces the
*next* cycle's defect classes; nothing in the build pushed back on the growth. The
mechanism-delta ratchet is that counter-pressure. It is modelled directly on
`scripts/check_complexity_baseline.py` — same shrink-only shape, same four mutually-exclusive
buckets (`active` / `new` / `increased` / `stale`), same `has_regression = new or increased`.

`make lint` runs `python scripts/check_mechanism_delta.py --check`, which censuses the tree
and compares it against `.github/mechanism-baseline.json`. Adding a mechanism fails;
**removing one always passes** (it buckets as `stale`). That asymmetry is the whole point: a
freeze would block legitimate work, a ratchet only makes growth cost a written justification.

### The seven kinds

The kinds **partition** the surface: every definition site yields exactly **one**
`(kind, name)` entry, so no mechanism needs two justifications and none can hide in a gap
between kinds.

| kind | what is detected | name |
|---|---|---|
| `lock` | `ast.ClassDef` matching `.*Lock.*`, and string literals matching `\.lock$`, under `src/` and `scripts/` | the class name, or the lock filename |
| `env_var` | `scripts/gen_env_registry.py`'s fail-closed `scan()`, filtered to `REBAR_*`, **unioned** with the two families `scan` cannot see (env-channel aliases from `rebar._deprecations.REGISTRY`, and the derived `REBAR_MCP_*` from `rebar.mcp_server.MCP_ENV_VARS`) exactly as `render()` unions them | the variable name |
| `config_key` | the **non-boolean** remainder of `rebar._config_sections._SECTIONS` | `<section>.<key>` |
| `feature_flag` | the `_SECTIONS` entries whose coercer mentions `_as_bool` | `<section>.<key>` |
| `ci_gate` | `scripts/check_*.py`, plus every `.github/workflows/` step carrying a `run:` | the script path, or `<workflow>::<step>` |
| `autouse_fixture` | a decorator resolving to `pytest.fixture` carrying `autouse=True`, under `tests/` | `<path>::<fixture>` |
| `test_helper` | `tests/_*.py` | the file path |

Two consequences of the partition rule are load-bearing rather than cosmetic. **`feature_flag`
claims the boolean keys and `config_key` claims only the remainder** — counting a boolean key
as both would demand two justifications for one definition site, which a per-kind marker
cannot express. And **config names are section-qualified**, never the bare key: `_SECTIONS`
repeats key names across sections (`allow_insecure` in both `reconciler` and `jira`,
`threshold` in both `ticket_clarity` and `compact`), so a bare-key baseline would silently
merge four definition sites into two entries — and a fifth could then be added for free.

### The marker

```
# mechanism-ok: <kind> <name> — <reason or ticket id>
```

It admits **exactly** the `(kind, name)` it names, never its whole kind, so a second lock does
not ride in on the first one's justification. A **blank reason is itself an error**: an
unexplained marker is indistinguishable from a rubber stamp (the same lesson the
tickets-boundary gate learned, where seven of thirteen sanctioned defects carried a bare
marker). Placement follows the detection shape, and each detector scans exactly its own:

| shape | kinds | where the marker may sit |
|---|---|---|
| Python definition line | `lock` classes, `autouse_fixture`, `config_key`, `feature_flag` | that line, or the one before it |
| string literal | `lock` filenames, `env_var` | the literal's line or the one before it, in any attributed file |
| filename glob | `ci_gate` scripts, `test_helper` | the matched file's first 20 lines |
| YAML step | `ci_gate` workflow `run:` steps | the step's `- name:`/`run:` line, or the one before it |

### Layout and maintenance

The entrypoint is `scripts/check_mechanism_delta.py`; the detectors live in the private
`scripts/_mechanism_delta/` subpackage, split **by input surface** — `detect_code.py` (Python
AST), `detect_config.py` (the config registries), `detect_ci.py` (globs and YAML) — plus
`baseline.py`, `compare.py` and `markers.py`. Seven detectors in one file would breach both
the module-size cap and the complexity ceiling; a flat `{kind: callable}` dispatch table keeps
the entrypoint's own complexity near 4. The subpackage is private because
`scripts/check_import_walk.py` imports every top-level `scripts/*.py` standalone with the
scripts directory stripped from `sys.path`, and a subpackage is invisible to that walk.

`--update-stale` drops entries whose definition site is gone and rewrites canonical sorted
JSON. It **refuses to write** — leaving the baseline byte-identical — while anything is new or
increased, so it can never bless a regression into the baseline. It is maintenance-only; a
contributor's path is the marker, or removing the mechanism.

The gate is stdlib plus PyYAML (already a dev dep, used read-only) and needs **no CI
provider**: it runs identically from `make lint`, a pre-commit hook, or a bare shell, so a
checkout with no CI at all still gets it (`project.portability`).

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
