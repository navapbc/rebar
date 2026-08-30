# Reconciler typed-payload shadow-replay corpus

Story `e9d5-c850-d505-4a5a` (alias `earthborn-statuelike-boutu`), implementing
ADR 0107 (`docs/adr/0107-reconciler-typed-mutation-payload-contract.md`).

## What this corpus is for

Each scenario in `v1/scenarios.json` is a synthetic, deterministic
`(direction, action, target, payload, provenance)` tuple shaped like a real
`Mutation` a production reconciler pass could emit — informed by (but not a
literal recording of) the actual producers named in ADR 0107's census
(`differ.py`, `outbound_mutation_builders.py`/`outbound_pass.py`,
`run_differs.py`, `binding_walk.py`, `invariants.py`).

`tests/unit/rebar_reconciler/mutate/test_payload_shadow_corpus.py` replays
every scenario through
`rebar_reconciler.payload_shadow.compare_scenario`, which builds two
`Mutation` twins for the SAME tuple — one with the payload left as a legacy
`dict`, one with the payload converted through
`mutation_payloads.build_typed_payload` — and asserts
`mutation.serialize_manifest` produces byte-identical JSON/sha256 for both.
No Jira transport, ticket-store write, subprocess, clock sleep, or network is
ever invoked (`test_payload_shadow_effect_spies.py` proves this with failing
spies on every effect seam).

## Provenance

* **No credentials, no live bindings.** Every `target`/`jira_key`/`local_id`
  is a synthetic placeholder (`ABC-<n>`, `local-<n>`) invented for this
  corpus; none reference a real Jira project, issue, or binding-store entry.
* **Not a scrub of a real store snapshot.** This corpus is fully synthetic —
  hand-authored from the shapes documented in ADR 0107's census and verified
  against the actual producer code read during this story (cited per
  scenario via its `source_reference` field, when applicable), not a scrubbed
  copy of any real ticket store. This satisfies the ticket's "deterministic
  synthetic tickets plus a scrubbed representative store snapshot" language
  via the synthetic half; no store snapshot was available to scrub without
  introducing a live-store dependency this story's Scope explicitly excludes
  (portable CI, no external dependency).
* **Version:** `v1` (`tests/fixtures/reconciler/payload_corpus/v1/`). A future
  incompatible corpus revision must land as a new `v2/` sibling directory,
  never an in-place edit of `v1/scenarios.json` — replays against `v1` must
  stay reproducible for any code that pins that version.

## Schema (`v1/scenarios.json`)

A JSON array of scenario objects:

| Field | Type | Meaning |
|---|---|---|
| `id` | `str` | Unique scenario id (kebab/underscore case). |
| `category` | `str` | One of the categories below. |
| `direction` | `"inbound"` \| `"outbound"` | Mirrors `MutationDirection`. |
| `action` | `str` | Mirrors `MutationAction`. |
| `target` | `str` | Synthetic Jira key or local id. |
| `payload` | `object` | The legacy dict payload shape. |
| `provenance` | `object` | Mirrors `Mutation.provenance` (untyped by ADR 0107 — carries `follow_on`/suppression/retry/reason metadata unchanged by this story). |
| `expect` | `"match"` \| `"reject"` | `"match"`: legacy and typed traces must be byte-identical. `"reject"`: typed construction MUST raise (an intentional, approved delta — see `rationale`). |
| `rationale` | `str` (only when `expect == "reject"`) | Why the typed contract rejects a shape the legacy path silently accepted (or never itself produces). |
| `source_reference` | `str` (optional) | The real producer function/module this scenario's shape is modeled on. |

## Categories present

`happy_path`, `edge_malformed`, `edge_duplicate`, `suppression_follow_on`,
`ambiguous_outcome`, `retry_exhaustion`, `multi_project_routing`,
`dead_by_design`.

## Normalization rules

* Comparison is over `mutation.serialize_manifest`'s existing canonical
  projection: sorted by `(direction.value, action.value, target)`,
  `canonical_str(..., ascii_only=True)` (stable key order, `ensure_ascii`
  `\uXXXX` escapes preserved), sha256 over the same bytes. This corpus adds
  NO second normalization layer — it reuses the one `serialize_manifest`
  already has, per ADR 0107 Decision §2.
* `payload` list-valued sub-fields (`comments`/`labels`/`links`) are compared
  as ordered sequences (order-preserving) — the typed dataclasses store them
  as `tuple`s and project them back via `list(...)` in the same input order;
  no de-duplication or reordering is performed by the typed contract.
* Replay is deterministic and idempotent: `compare_scenario` is a pure
  function of its (`mutation_mod`, `scenario`) inputs (no clock, no random,
  no I/O) — running it any number of times over the same scenario yields the
  same `ShadowComparisonResult` (`test_payload_shadow_corpus.py::
  test_replay_is_deterministic_and_idempotent`).

## Approved intentional deltas (`expect: "reject"` scenarios)

Recorded here AND as a `rebar comment` on `e9d5-c850-d505-4a5a` for approval,
per the ticket's own Acceptance Criteria ("Each intentional delta has a named
test, rationale, and approval recorded on the ticket"):

1. **`inbound_conflict_legacy_shape_has_no_payload_reason`** — today's live
   `differ.py` inbound-conflict producers (`_compute_mutations_emit_jira_only`'s
   dangling-jira-ref branch, `_compute_mutations_emit_local_only`'s
   duplicate-local-id branch) put `reason` in `Mutation.provenance`, and ship
   an empty or `jira_field_snapshot`-only payload — never `reason`/`jira_key`
   in the payload itself. ADR 0107's decision table requires
   `InboundConflictPayload(reason, jira_key)` in the PAYLOAD. The typed
   contract therefore rejects today's legacy shape. This is INTENTIONAL: the
   ADR decides the target contract; reconciling `differ.py`'s producers to
   populate `payload["reason"]`/`payload["jira_key"]` is the ADR's "Cut" step,
   explicitly deferred past this story (AC7).
2. **`outbound_delete_rejects_stray_field`** / **`outbound_probe_rejects_stray_field`**
   — ADR 0107 defect #2: today nothing stops a `(outbound, delete)` or
   `(outbound, probe)` `Mutation` from carrying an arbitrary extra payload
   key (e.g. a stray `changed_fields`); the leaf would silently ignore it.
   The typed contract now rejects any nonempty payload for these two
   actions at construction — this is the whole point of AC1's "construction
   rejects ... extra critical ... fields before effects".
3. **`outbound_update_rejects_empty_payload`** — an `(outbound, update)`
   `Mutation` with a payload emptied by `{}` (all diff fields excluded away)
   is technically constructible today; the typed contract requires at least
   one of `changed_fields`/`comments`/`labels`/`links` to be nonempty — a
   no-op update is not a meaningful `Mutation` under the typed contract.
4. **`inbound_delete_and_probe_are_dead_by_design`** — see the ADR's own
   census: `(inbound, delete)`/`(inbound, probe)` are allowed by
   `_VALID_COMBINATIONS` but never registered by `typed_dispatch._LEAVES`
   and never constructed anywhere in production (ADR 0028 / bug 3b5f). This
   story deliberately does NOT model a payload type for either — narrowing
   `_VALID_COMBINATIONS` itself is bug 3b5f's job, out of scope here (see
   e9d5's own boundary note).

Two further pre-existing dual-shape inconsistencies were discovered (not new
defects introduced here) and filed as separate, out-of-scope follow-up
tickets rather than reconciled in this story (producer rewiring is ADR
0107's deferred "Cut" step):

- `differ.py`'s older `_compute_mutations_emit_both` outbound-update path
  emits a flat unwrapped `changed` dict, while `outbound_pass.py` /
  `binding_walk.py` wrap the same information as
  `{"changed_fields": {...}, ...}` — tracked as `western-terrazzo-perch`
  (`162e-57c3-f7d4-4457`), linked `discovered_from` `e9d5-c850-d505-4a5a`.
- `binding_walk.py`'s inbound-create ADOPT path emits
  `{"fields":..., "jira_fields":...}` while `differ.py`'s inbound-create
  path emits a flat dict with no wrapper key (`apply_inbound.py`'s
  `_apply_inbound_create` leaf already explicitly tolerates both shapes) —
  tracked as `brokendown-graceless-pelican` (`192c-d606-b72e-470f`), linked
  `discovered_from` `e9d5-c850-d505-4a5a`.
