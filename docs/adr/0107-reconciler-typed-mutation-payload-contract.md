# ADR 0107 — Reconciler typed mutation payload: a discriminated union by `(direction, action)`

- **Status:** Accepted (2026-08-30, human operator review via chat session; explicit
  approval to proceed given for `earthborn-statuelike-boutu`, the dependent implementation
  story). Moved from Proposed/DRAFT per the architecture-decision RESEARCH → DRAFT → HOLD →
  MERGE lifecycle — the record's reasoning (census, contract enumeration, and disposition of
  the two dead-by-design combinations) was reviewed and is approved as-is with no changes.
- **Context:** Story *Specify the typed reconciler payload and cutover contract*
  (`f448-b336-e1c7-4ead`, alias `scummy-ultrashort-tuatara`); epic *Converge reconciler,
  runner, and operation boundaries* (`eb64-844b-ab7a-4915`, alias
  `financial-stoneware-brant`), Outcome A.

## Context

`rebar_reconciler.mutation.Mutation` already has typed `direction: MutationDirection` and
`action: MutationAction` fields, and `_VALID_COMBINATIONS` (an explicit allowlist) already
constrains which `(direction, action)` pairs may exist. `typed_dispatch._LEAVES` already
routes each pair to one leaf handler, one per valid combination. What remains untyped is
`Mutation.payload: Mapping[str, Any]` — the actual field contents differ per `(direction,
action)` pair, are validated only informally inside each leaf, and are round-tripped through
a second, parallel legacy shape (`batch_dispatch._mutation_to_batch_dict`) for every outbound
create/update because the legacy `_apply_batch` path (still production for those two
actions) only understands dicts.

Three specific defects motivate this design:

1. **Two CREATE payload shapes coexist** in `_mutation_to_batch_dict` (nested `"fields"` key
   for legacy/fixture callers vs. top-level spread for `reconcile.py`'s production callers),
   distinguished at runtime by `"fields" in payload` — an implicit, undeclared contract.
2. **`Mutation.payload` cannot express "invalid combination of fields for this action"** at
   construction time — e.g. an `(outbound, delete)` Mutation could technically carry a
   `changed_fields` key and nothing would reject it before the leaf silently ignores it.
3. **The outbound-update producer seam (`_compute_outbound_update_mutation`) has 23 discrete
   parameters** (17 positional + 6 keyword-only, at both the definition in
   `outbound_mutation_builders.py` and its sole call site in `outbound_differ.py` — the
   ticket's "24-value" description is this seam counted inclusive of its `mutations`
   accumulator argument), with two field-diff collaborators of 14 (`compute_update_fields`)
   and 11 (`diff_canonical_fields`) parameters respectively. This is loose-parameter debt
   independent of, but adjacent to, the payload-typing problem.

## Census — every producer, consumer, string/dynamic-load, and manifest reference

Performed via Serena (`find_referencing_symbols`/`find_symbol`, semantic) **and** `grep`
(string/monkeypatch/dynamic-import), per this project's own asymmetric-tool convention
(`docs/code-navigation.md`): Serena omits string-named and `Any`-typed-receiver references,
so an empty Serena result was in every case corroborated (or overturned) by a text search
before being treated as "no reference."

### `(direction, action)` combinations

`_VALID_COMBINATIONS` (the allowlist in `mutation.py`) permits **12** pairs: 5 outbound
(`create`, `update`, `delete`, `probe`, `conflict`) × 7 inbound (`create`, `update`,
`clean_label`, `repair_property`, `conflict`, plus **`delete` and `probe`**, which are
inbound-legal by the allowlist's construction — outbound excludes only
`{clean_label, repair_property}`, nothing excludes inbound `delete`/`probe`).

`typed_dispatch._LEAVES` registers only **10** of those 12 — it has no entry for
`(inbound, delete)` or `(inbound, probe)`. Grepping every `Mutation(...)`/`MutationAction.*`
construction site in `src/rebar/_engine/rebar_reconciler/` confirms **neither combination is
ever constructed**: `MutationAction.probe` is only ever paired with
`MutationDirection.outbound` (`differ.py:410-411`, the "ambiguous local binding" leaf), and
`MutationAction.delete` is only ever paired with `MutationDirection.outbound`
(`run_differs.py:602-604`, the outbound-differ delete branch). `apply_inbound.py`'s own module
docstring documents why: *"Bug 3b5f removed the `delete` and `probe` inbound leaves: their
producer chain had no live emitter, and `_apply_inbound_delete`'s
`create_after_hard_delete` follow-on WAS the resurrection of a deliberately-deleted Jira
issue that the operator ruling forbids. A confirmed hard-delete now tombstones the pairing in
`outbound_differ` instead — see ADR 0028's Consequences."* — i.e. these two combinations are
**dead by deliberate design decision**, not an oversight, and their continued advertisement in
`_VALID_COMBINATIONS` is stale.

**Disposition:** the typed payload union defines a payload type for exactly the **10 live**
combinations. `_VALID_COMBINATIONS` should be narrowed to drop `(inbound, delete)` and
`(inbound, probe)` as part of the implementation story (`earthborn-statuelike-boutu`) — this
is a concrete, evidence-backed deletion target, not new design surface.

### Producers (construct a `Mutation`)

| Site | Combinations produced |
|---|---|
| `differ.py` (`_compute_mutations_emit_*`) | `(outbound, create)`, `(outbound, probe)`, `(inbound, create)`, `(inbound, update)`, `(outbound, update)`, `(*, conflict)` via `conflict_resolver` |
| `outbound_mutation_builders.py` (`_compute_outbound_create_mutation`, `_compute_outbound_update_mutation`) | Builds `OutboundMutation` (an intermediate, untyped-payload dataclass in `outbound_differ.py`), not a `Mutation` directly |
| `run_differs.py` (`_run_differs_outbound`) | Converts `OutboundMutation` → typed `Mutation` for `create`/`update`/`delete` |
| `invariants.py` (`check_dual_identity_complete`, via `seed_mutations`) | Repair/inbound mutations injected ahead of the differ's own walk |
| `apply_inbound.py` (`_apply_inbound_clean_label`/`_apply_inbound_repair_property`) callers upstream | `(inbound, clean_label)`, `(inbound, repair_property)` — constructed by whichever caller detects the drift (label/property audit paths) |

### Consumers (read `Mutation.payload`)

136 call sites read `.payload` across `src/rebar/_engine/rebar_reconciler/` and
`tests/unit|integration/rebar_reconciler/` (grep count). The production dispatch consumers are:

- `typed_dispatch._apply_typed` → one of 10 leaf handlers in `apply_outbound.py` / `apply_inbound.py`.
- `batch_dispatch._mutation_to_batch_dict` — the ONLY untyped-to-legacy-dict bridge; converts
  `(outbound, create)` and `(outbound, update)` Mutations (and any dict-shaped legacy mutation)
  into the `_apply_batch` dict shape. This is the single deletion target named by the epic.
- `applier.apply()` — the polymorphic entry point (`MutationShape` `isinstance` branch vs.
  legacy-dict/list branch); the `MutationShape` `Protocol` (`apply_base.py`) is the existing,
  declared, cross-module-reload-safe discrimination mechanism and should be REUSED, not replaced.
- `mutation.serialize_manifest` — canonical JSON + sha256 hash over
  `dict(m.payload)`, sorted by `(direction.value, action.value, target)`.

### String / dynamic-load / monkeypatch / manifest consumers

- **Dynamic loader (`_loader.lazy_load`)**: `mutation.py` is one of the location-pinned,
  by-filename-loaded modules named in ADR 0083 §(a) — `differ.py`, `apply_base.py`,
  `invariants.py` all resolve it via `lazy_load("rebar_reconciler.mutation", "mutation.py")` /
  equivalent, under the canonical `sys.modules["rebar_reconciler.mutation"]` key, **not** a
  normal import, in every production caller except `mutation.py` importers reached via the
  packaged `rebar_reconciler` namespace. `applier.py` additionally reloads this key across
  test boundaries (`test_rebar_id_label_writers.py` comment: *"canonical mutation module —
  REB-3115 S5 T1 removed [a fork]"*).
- **String-patched test doubles**: `mock.patch("rebar_reconciler.<mod>.<attr>", ...)` sites
  that touch mutation construction are concentrated in `tests/unit/rebar_reconciler/mutate/`
  (14 files) and `diffing/`/`conflict/`/`orchestrate/` (11 files) — see the full file list
  gathered by grep above; every one imports the CANONICAL `rebar_reconciler.mutation` key
  (never a relative/package-qualified alternate path), so a typed-payload change does not
  fragment patch targets as long as `Mutation`/`MutationAction`/`MutationDirection` keep their
  current module identity.
- **Manifest/serialization**: `serialize_manifest` is the one canonical-bytes boundary (see
  Decision, "compatibility bytes vs. comparison-only" below).
- **Compatibility re-exports**: `batch_dispatch.__all__` re-exports `_mutation_to_batch_dict`
  explicitly (so it is a declared public-ish symbol of that module, not accidental) — this
  confirms it as a first-class, single, well-known deletion target rather than an incidental
  private helper.
- **An empty Serena result was independently confirmed by `grep`** for every "as a string"
  category above (dynamic loader keys, `mock.patch` targets, `__all__` re-exports) — none of
  these are resolvable by Serena's semantic references, consistent with this repo's own
  documented Serena/grep asymmetry, so the grep pass was not optional.

## Decision

### 1. One discriminated payload type per live `(direction, action)` pair

This is not a novel pattern for this codebase — it follows three existing in-repo
discriminated-union precedents rather than inventing a fourth:

- **`mutation.py`'s own `MutationDirection`/`MutationAction` + `_VALID_COMBINATIONS`
  allowlist** is the closest and most direct precedent: an explicit enum pair plus an
  allowlist of valid combinations, exactly the shape this ADR extends one level deeper (from
  discriminating the *handler* to discriminating the *payload*).
- **`common.schema.json`'s `"kind"` evidence discriminator** (`{"required": ["kind"], "kind":
  {"enum": ["file", "url", "source"]}}`) is the schema-level precedent for "one required
  literal field selects which sibling shape applies," which is exactly what
  `(direction, action)` already does for `Mutation`.
- **`llm/usage_log.py`'s row-outcome discriminator** (`OUTCOME_OK`/`OUTCOME_FAILED`, an
  EXPLICIT field on every row rather than "failure = some field is missing") is the precedent
  for this ADR's own validation rule: a payload dataclass's absent optional field must encode
  "not applicable," never be overloaded to also mean something failed — the same discipline
  this ADR's `__post_init__` validation follows (see Decision §2).

Ten frozen, slotted dataclasses (mirroring `Mutation`'s own `@dataclass(frozen=True,
slots=True)` style), one per live combination:

| `(direction, action)` | Payload type | Required fields | Optional fields |
|---|---|---|---|
| `(outbound, create)` | `OutboundCreatePayload` | `fields: Mapping[str, Any]` (vendor create fields) | `comments: tuple[Mapping,...]`, `labels: tuple[Mapping,...]`, `links: tuple[Mapping,...]` (empty at emit time — resolved post-creation), `key_hint: str \| None` (legacy-route rollback only) |
| `(outbound, update)` | `OutboundUpdatePayload` | *(none — at least one of `changed_fields`/`comments`/`labels`/`links` non-empty, enforced by `__post_init__`)* | `changed_fields: Mapping[str, Any]`, `comments: tuple[...]`, `labels: tuple[...]`, `links: tuple[...]`, `local_id: str` |
| `(outbound, delete)` | `OutboundDeletePayload` | *(none — the Jira key lives in `Mutation.target`)* | — |
| `(outbound, probe)` | `OutboundProbePayload` | *(none — the ambiguity reason lives in `Mutation.provenance`, not payload)* | — |
| `(outbound, conflict)` | `OutboundConflictPayload` | `reason: str` | `local_id: str` |
| `(inbound, create)` | `InboundCreatePayload` | `fields: Mapping[str, Any]` (jira-shape scalar fields) | `status: str \| None` |
| `(inbound, update)` | `InboundUpdatePayload` | `fields: Mapping[str, Any]` | `local_id: str \| None` (bug-1bb2 override), `status: str \| None`, `labels: tuple[...]`, `comments: tuple[...]`, `links: tuple[...]` |
| `(inbound, clean_label)` | `InboundCleanLabelPayload` | `labels_to_remove: tuple[str, ...]` | — |
| `(inbound, repair_property)` | `InboundRepairPropertyPayload` | `local_id: str` | — |
| `(inbound, conflict)` | `InboundConflictPayload` | `reason: str`, `jira_key: str` | `local_id: str` |

No eleventh "generic action payload" type exists, and no field is optional on more than the
combinations that already tolerate its absence today (verified against each leaf's actual
`payload.get(...)` calls in the census above) — this is the AC2 requirement.

### 2. Ownership of validation, serialization, projection, follow-ons, binding identity, outcomes

- **Field validation**: each payload dataclass's own `__post_init__`, exactly like
  `Mutation.__post_init__` validates `target`/`payload`/`provenance` types today. No new
  central validator module.
- **Canonical serialization / comparison**: `serialize_manifest` keeps its existing contract
  (sort by `(direction.value, action.value, target)`, `canonical_str(..., ascii_only=True)`,
  sha256 over the same). It gains exactly one responsibility: call a new
  `payload.as_legacy_dict()` method (present on every payload type, including the two
  bookkeeping-only ones, which return `{}`) instead of `dict(m.payload)`, so the **emitted
  JSON bytes for any already-shipped `(direction, action, target)` triple are unchanged** —
  this is the "compatibility bytes" requirement (AC3): `serialize_manifest`'s current sha256
  is a compatibility requirement (external tooling/tests may already store or diff these
  hashes); the in-memory *typed* representation is comparison-only and may differ in shape
  from the dict as long as `as_legacy_dict()` round-trips byte-identically for every
  already-shipped triple.
- **Adapter projection** (vendor field mapping): stays owned by the existing
  `OutboundMapper`/`InboundMapper` ports (`outbound_mapper.map_local_to_remote` /
  `map_fields_to_remote`, `inbound_mapper.map_remote_to_local`) — the payload types carry
  vendor-shaped `fields` (as they do today); they do not duplicate or wrap the mapper.
- **Follow-ons / suppression**: stay expressed via the existing untyped `follow_on: dict`
  convention returned on `ApplyResult.payload` (e.g. `{"kind": "suppress_pair", ...}`) — this
  ADR does NOT promote `follow_on` to a discriminated field. `ApplyResult` is an *outcome*,
  not a `Mutation` payload, and is out of this story's scope; widening it here would be scope
  creep the epic constraints (no new generic option-bag) explicitly warn against.
- **Binding identity**: stays owned by `binding_store` (unaffected — Mutations never carry
  binding state, only `local_id`/`jira_key` strings already present in some payload fields).
- **Provider-neutral outcomes**: stay owned by `ApplyResult` (`apply_base.py`, unaffected by
  this ADR).
- **Retry / idempotency / fuse ownership is explicitly REAFFIRMED to remain solely with ADR
  0103's `RetryBudget`/coordinator.** No payload type or adapter introduces a second retry
  owner, a second backoff schedule, or a second observe-before-replay decision table. This
  satisfies AC4 by construction — the payload types carry no retry state at all.

### 3. Disposition of the 23/14/11-parameter producer seams (AC7)

**Rejected: a new generic context/options object.** Wrapping the 23 `_compute_outbound_
update_mutation` parameters (or the 14/11-parameter collaborators) in one new dataclass would
violate the epic's explicit constraint ("no generic option-bag replacement") and would not by
itself reduce coupling — it would just rename the same 23 names as attributes of one bag,
which is exactly the anti-pattern already rejected once in this codebase (`OutboundDiffConfig`
consolidated 5 of these; the ticket's phrasing "reject a wrapper that merely repackages the
same loose bundle" targets precisely this failure mode; the epic's parallel Outcome B
constraint against a "second generic context" for the runner makes the same call for that
subsystem).

**Decided disposition** — the 23/14/11 parameters group into four EXISTING cohesive owners,
not one new one:

1. **Pass-scoped values** (`pass_id`, `prev_snapshot`, `absent_alive_fields`,
   `_selected_for_get_this_pass`, `local_label_intent`, `links`, `mapping`, `repo_root`,
   `effective_cache`) belong to `OutboundDiffConfig` (already the pass-level optional-input
   collapse point in `outbound_differ.py`) or to `Observation`/`ObservationVersion`
   (`observation.py`, the frozen provider-neutral pass-input snapshot) — whichever the
   implementation story finds already threads through the caller at that point; both are
   EXISTING owners named in the epic's "Reuse `OperationSnapshot`... `Observation`,
   `TicketPlan`" constraint.
2. **Per-ticket values** (`ticket`, `status`, `local_id`, `jira_key`, `jira_snapshot`,
   `local_parents`, `local_ticket_types`) belong to `TicketPlan` (`ticket_plan.py`) once it is
   populated for the outbound path — it already carries per-ticket lifecycle/parity state for
   the boutu-side shadow work; threading the outbound builder through it (rather than 7
   separate parameters) is additive, not a new type.
3. **Backend-port collaborators** (`binding_store`, `client`, `outbound_mapper`,
   `inbound_mapper`, `_assignee_resolver`) are unaffected — they are already single,
   named, narrow collaborators (the Backend port), not loose values.
4. **Observability sinks** (`conflict_sink`, `dropped_field_sink`) stay as explicit narrow
   parameters — each has exactly one purpose and is already documented on
   `OutboundDiffConfig`'s docstring; merging them into a bag would only obscure their
   single-purpose nature.

**This story documents the disposition only. The parameter-count reduction itself — actually
re-threading `_compute_outbound_update_mutation` through `OutboundDiffConfig`/`Observation`/
`TicketPlan` — is explicitly deferred to `earthborn-statuelike-boutu`**, which the epic's
2026-08-29 decomposition comment already schedules to "add the direction/action union into
these same files." Doing the parameter motion here, ahead of the typed-payload expand, would
create exactly the kind of double-move ADR 0083 warns against for physically-pinned modules.

### 4. Migration sequencing (AC6)

This is an application of rebar's own documented **expand/contract** migration practice
(`docs/migrations.md`; the same vocabulary and 6-step shape ADR 0060 names explicitly as
prior art, ADR 0098 §"Migration and rollback" spells out step-by-step for the LLM-config
cutover, and ADR 0100 applies per command family), not a new migration mechanism — this ADR
does not invent a fifth migration pattern. "Expand" below is rebar's expand half (additive,
shadow-mode, no behavior change); "compare" + "cut" together are the contract half's
proof-then-flip steps; "delete" is the contract half's final legacy-removal step. This also
mirrors the `create_route()` precedent already landed in this same package (one selector, one
rollback value, never dual-send):

1. **Expand** (additive only): land the 10 payload dataclasses + `as_legacy_dict()`
   projection methods. `Mutation.payload` keeps its `Mapping[str, Any]` type (unchanged
   signature) — a payload dataclass is itself a `Mapping`-compatible object (or the
   constructor accepts either shape) so existing untyped-dict callers keep working
   unmodified during expand. No behavior change; `_mutation_to_batch_dict` and every leaf
   handler are untouched in this phase.
2. **Compare** (shadow, side-effect-free): replay the portable synthetic/scrubbed corpus
   through both the legacy dict-payload path and the new typed-payload construction path;
   assert `serialize_manifest` produces byte-identical JSON/hash for every triple. This reuses
   the EXISTING `OutboundDiffConfig(client=None)` no-I/O comparison mode — no second test
   harness. Any intentional delta (e.g. a field the typed contract now REJECTS that the
   legacy path silently accepted) must be enumerated and approved, per AC2's parent
   Acceptance Criterion, before cutover.
3. **Cut** (production route flip): producers (`differ.py`, `outbound_mutation_builders.py`,
   `run_differs.py`) construct native typed payloads directly instead of raw dicts. Live
   comparison, if performed, uses isolated Jira DC scratch projects (the existing
   `tests/external/live_jira_dc` harness) — never dual-sends to one issue (epic constraint;
   AC5).
4. **Delete** (same change as cut, not a follow-up): `_mutation_to_batch_dict`'s two-CREATE-
   shape branch, the untyped-dict acceptance branch inside `applier.apply()`'s `MutationShape`
   dispatch, and any now-dead compatibility tests that assert the legacy dict shape
   specifically. `MutationShape` itself (the `Protocol`) is RETAINED — it is the declared,
   cross-reload-safe discrimination mechanism the typed payloads continue to satisfy; only its
   untyped-dict FALLBACK branch is deleted. Exit criterion: zero remaining constructors of a
   bare dict `Mutation.payload` in production code (test fixtures for legacy-shape regression
   may remain until AC's "typed-only convergence pass," per the epic's chain to
   `single-vast-roan`).

### 5. Reconciliation with existing ADRs

- **ADR 0004** (snapshot contract): unaffected — that ADR governs the FETCHER's output shape
  (`JiraSnapshotEntry`), not the Mutation payload; no conflict.
- **ADR 0026/0027/0028/0029** (three-way merge, binding lifecycle, bound-but-absent, status
  echo): unaffected — these are FIELD-ARBITRATION policy decisions consumed as inputs to
  payload construction (e.g. `diff_canonical_fields`'s baseline arbitration); the typed
  payload carries the OUTPUT of that arbitration (`changed_fields`), never re-decides it.
- **ADR 0083** (vendor-adapter seam): the dynamic-loader location-pinning constraint is
  respected — `mutation.py` stays at its current path; this ADR adds new dataclasses to that
  SAME file (or a sibling `mutation_payloads.py` the implementation story may choose), not a
  new sub-package, so no loader/patch-string migration is triggered.
- **ADR 0092** (bridge-primary vocabulary compat adapters): unaffected — vocabulary/value
  translation stays in the vendor mapper ports, per Decision §2 above.
- **ADR 0103** (operation coordination): explicitly reaffirmed, not reopened — see Decision
  §2's retry-ownership clause.

**None of the six reconciled ADRs decide the Mutation-payload representation boundary
itself** — confirming AC8's "otherwise the evidence records why accepted ADRs already decide
the shape" branch does NOT apply here, and a new ADR (this one) is required, consistent with
the story's own Scope note ("one atomic decision record only if accepted ADRs do not already
decide the representation boundary").

## Consequences

- Every valid `(direction, action)` pair has one named, validated payload contract; invalid
  field combinations become `__post_init__` `ValueError`s instead of silently-ignored extra
  dict keys.
- The two-CREATE-shape ambiguity in `_mutation_to_batch_dict` is named as a concrete,
  scheduled deletion target rather than perpetuated indefinitely behind a `"fields" in
  payload` runtime branch.
- `_VALID_COMBINATIONS` is flagged for narrowing (drop the two dead inbound combinations) —
  a small, evidence-backed, low-risk cleanup that shrinks the union's surface before the
  typed cutover, rather than typing two combinations nothing constructs.
- The 23/14/11-parameter producer seam is NOT re-threaded by this story; its disposition is
  recorded so `earthborn-statuelike-boutu` does not have to re-derive it, and so it does not
  invent a fifth generic bag when four existing owners already cover every parameter.

## Rollback

Additive through the "expand" phase (step 1) — reverting is a straight revert of the new
dataclass file(s), zero blast radius on production behavior. Once "cut" (step 3) lands,
rollback is the same single-selector flip pattern `create_route()` already established
(flip back to constructing legacy dicts) — provided "delete" (step 4) has not yet also
landed; after deletion, rollback requires re-adding the deleted legacy path from version
control, exactly as any other one-way-door deletion in this codebase.

## Open items for the implementation story

- Confirm whether the ten payload dataclasses live in `mutation.py` itself (kept small,
  single-purpose, ~10 classes) or a new sibling module — this is an implementation-story
  choice, not a structural decision this ADR needs to pin, since either choice satisfies
  ADR 0083's location-pinning constraint as long as the loader is updated in the same change
  if a new file is chosen.
- Confirm the exact `inbound_repair_property` payload shape end-to-end once the implementing
  agent has the leaf's live-client integration test in hand — this record traced its
  documented contract (`local_id` via `client.set_issue_property`) but did not execute the
  live-client path.
