# The reconciler mapping seam

The mapping seam makes the reconciler's rebar↔target vocabulary translation **config-driven**
instead of hardcoded, while keeping the mapping engine **provider-neutral** so a future target
(Linear, GitLab, GitHub Issues) can reuse it. This page is the end-to-end guide: the
provider-neutral core, the built-in defaults and effective resolvers, the decision paths
(skip / drift / collapse, transition replay, and the parent breadcrumb), and the
`suggest-mapping` probe.

The **configuration surface** — the `[mapping]` / `[tool.rebar.mapping]` TOML shape, the five
axis maps, the four vocabulary declarations, and the three-layer merge order — is documented in
[config.md](config.md#provider-neutral-reconciler-mapping-mapping). Read that first for the
authoring model; this page covers the code behind it.

## Provider-neutral core vs Jira-adapter split

The mapping engine is deliberately split so the reusable half carries no vendor knowledge.

**The provider-neutral core** — `src/rebar/_engine/rebar_reconciler/mapping_config.py` — imports
**nothing** from any `adapters.jira*` package and contains **no Jira value literal** anywhere
(its docstring examples use neutral placeholders such as `"VALUE"` and `"<status-a>"`). It
provides:

- `MappingLayer` — one layer of mapping data (a built-in default, the `[mapping.default]` block,
  or one `[mapping.projects.<KEY>]` overlay). The five axis maps each default to an empty
  mapping; the four vocabulary declarations each default to `None`, meaning "undeclared —
  inherit the next-outer layer", which is distinct from an empty list (an EMPTY vocabulary).
- `MappingConfig` — the parsed overlay: the `default` layer plus the per-project `projects`
  overlays. An absent section yields an empty `default` and empty `projects` — a no-op overlay.
- `load_mapping_config(root)` — reads and parses the reserved `[mapping]` section, failing
  closed with `MappingConfigError` on any malformed block.
- `resolve_for_project(config, project_key, *, builtin)` — resolves the effective layer:
  axis maps deep-merge **per key** (`builtin ← default ← projects[key]`); vocabulary
  declarations replace **wholesale**, most-specific-declaring layer wins.
- `validate(resolved, capability)` — offline, config-only validation (never the network).

**The Jira adapter** injects the vendor-specific data the core needs: the built-in default
vocabulary layer (via the `builtin=` argument to `resolve_for_project`) and a `Capability`
descriptor describing the target. The core never reaches back into the adapter; the dependency
direction is one-way (concrete backends import the core, never the reverse).

### The capability descriptor

`Capability` (a frozen dataclass) records which mapping **axes** the target's vocabulary
actually supports — `has_types`, `has_transitions`, `has_hierarchy`, `has_link_types`,
`has_priorities`. Every axis **defaults to present** (a fully-capable target), so an unspecified
capability never spuriously gates a configured map. It is **data about the target**, injected by
the concrete adapter; the core stays neutral.

Capability-absent axis references **fail closed**: `validate` raises `MappingConfigError` when a
non-empty `type_map` is configured but the target has `has_types=False`, likewise `link_map`
against `has_link_types`, `priority_map` against `has_priorities`, and a declared `hierarchy`
against `has_hierarchy`. `status_map` and `create_defaults` are ungated. (`has_transitions`
gates a future axis that has no map here yet.)

## Built-in defaults and effective resolvers

`src/rebar/_engine/rebar_reconciler/config.py` holds the **built-in default literals** and the
**effective resolvers** that overlay config onto them. That module imports nothing but
`__future__` at module scope, so its operator-overridable literals never sit behind an adapter
or config import; `mapping_config` is imported **lazily** inside each resolver.

The built-in forward literals are:

- `local_to_jira_status` — local status name → Jira status (`open → "To Do"`, etc.).
- `LOCAL_TYPE_TO_JIRA` — local ticket type → Jira issue-type name.
- `local_to_jira_link` — local relation → Jira issue-link type name.
- `local_to_jira_priority` — local priority (string key `"0"`–`"4"`) → Jira priority name.

Each of these is a **second, independent literal** of a mapping whose sole adapter-side
definition lives under `adapters/jira_family/value_maps`; a parity test keeps the two honest
rather than an import (which would invert the one-way core←adapter layering).

The effective resolvers overlay the `[mapping]` config onto those built-ins, keyed by Jira
project KEY, through the same S1 three-layer per-key merge:

| Resolver | Built-in | Behavior |
| --- | --- | --- |
| `effective_status_map` | `local_to_jira_status` | `SKIP` or absent key → local status has no Jira target (drops the field: map-or-drift). |
| `effective_type_map` | `LOCAL_TYPE_TO_JIRA` | `SKIP` → type-granular skip (surfaced via `effective_excluded_sync_types`). |
| `effective_link_map` | `local_to_jira_link` | Validated against declared `link_types`; an out-of-vocabulary link **fails closed**. |
| `effective_priority_map` | `local_to_jira_priority` | Soft axis: no vocabulary, no `validate` call — an unmapped priority simply drifts. |
| `effective_create_defaults` | `{}` (empty) | Per-project create-time field defaults; CREATE-only, ungated. |
| `effective_excluded_sync_types` | `EXCLUDED_SYNC_TYPES` | The built-in excluded set (`session_log`, `code_review`, `identity`) UNION every type mapped to `SKIP`. |

**With no `[mapping]` block, each `effective_*` equals its built-in verbatim** — the config seam
is inert until configured.

### The completeness gate

`assert_type_decisions_complete(project_key)` is a **fail-closed gate**: every *syncable* local
ticket type (a `rebar.types.TicketType` member minus `EXCLUDED_SYNC_TYPES`) must be **decided** —
either present in `effective_type_map` (a Jira target) or in `effective_excluded_sync_types`
(mapped to `SKIP`). An undecided type would be silently coerced to a default Jira type
downstream; this gate raises `MappingConfigError` naming the undecided type(s) instead, so an
incomplete `[mapping.*.type_map]` (or a newly added `TicketType` with no sync decision) fails
loudly and up front. The built-in `LOCAL_TYPE_TO_JIRA` covers all four syncable types, so with
no `[mapping]` block this never fires.

## Skip, drift, and collapse decision paths

The resolvers encode three ways a local value reaches (or does not reach) Jira:

- **Skip** — an explicit `SKIP` sentinel value (`mapping_config.SKIP`, the string `"skip"`)
  drops the local key rather than mapping it. `SKIP` is **always** an allowed axis-map value
  regardless of the declared vocabulary. On the type axis, a skipped type is *excluded from
  sync* (`effective_excluded_sync_types`); on the status/link/priority axes, the field is
  dropped from the mutation.
- **Drift** — a local value with **no** target (an absent key, or a `SKIP` on a
  non-fail-closed axis) causes the caller to **omit the field** rather than coerce it. Status
  and priority are map-or-drift: a status/priority with no mapped target drifts rather than
  being forced to a default. Priority is the softest axis — it has no vocabulary declaration and
  no `validate` call, so an unmapped priority always simply drifts.
- **Collapse** — a per-project `type_map` (or other axis map) may map **two local values onto
  one Jira value** (e.g. two local types → `"Story"`). The built-in type map is bijective, so
  the reverse map (`jira_to_local_type`) is its exact inverse today; when an overlay collapses,
  the lossy direction is recovered from a `rebar-type:` annotation label, not from the Jira
  value alone. The status axis works the same way: the forward map is non-injective, so the
  canonical reverse is the *unannotated* local status, with `blocked`/`cancelled` reconstructed
  from `rebar-status:` annotation labels.

### Transition replay

Status changes are applied as Jira **workflow transitions**, not direct field writes. Because
the forward status map is non-injective (several local statuses map to `"In Progress"` or
`"Done"`), the reconciler routes each outbound status through the effective status map and
records the lossless local status via `rebar-status:` annotation labels emitted/removed by the
outbound status logic — so replaying inbound status back to local reconstructs the exact local
status rather than the collapsed workflow state.

### The parent breadcrumb (S7)

When a local ticket's parent hierarchy cannot be fully represented in Jira — because the direct
parent's type is **collapsed or absent** from Jira's issue-type hierarchy — the reconciler emits
an **echo-safe breadcrumb comment** noting the nearest tracked ancestor. This is
`_build_parent_breadcrumb` in
`src/rebar/_engine/rebar_reconciler/outbound_comments.py`. Its observable behavior:

- It fires only when the direct parent is present with a **non-epic** type (mirroring the
  parent-field suppression in the Jira outbound-fields mapper); a missing or epic parent type
  means the parent field is not suppressed, so no breadcrumb is emitted.
- It walks upward through local parents to the **nearest bound ancestor** (one with a bound Jira
  key) and names it in the comment; if one or more intervening levels were skipped for lacking a
  bound key, the comment additionally states that intervening levels are not represented.
- It is **append-once** and **echo-safe**: a stable identity tag
  (`<!-- rebar:parent-breadcrumb -->`) keys the dedup, so a re-sync recognises the existing
  breadcrumb and never appends a duplicate. Full parent context is always maintained in rebar;
  the breadcrumb only records that Jira's view is partial.

## The `suggest-mapping` probe

To seed a `[mapping]` block from a live Jira project instead of hand-authoring it, run:

```
rebar bridge suggest-mapping <PROJECT> [--write]
```

> **Renamed from `probe`.** This command was called `bridge probe` before S8. The verb was
> renamed to `suggest-mapping` because `bridge probe` collided with the retired
> throwaway-issue probe alias (which *created and deleted* a throwaway issue). Operators who
> knew the old `probe` name should use `suggest-mapping` — it is strictly read-only.

The command inspects a live Jira project through a narrow, **read-only** port and emits a
suggested `[mapping.projects.<KEY>]` block seeded with the project's real vocabulary
(`issue_types` / `statuses` / `link_types` / `hierarchy`) and **identity-seed** axis maps (every
local key mapped to the same remote value, so the operator edits only where a local name must
diverge). Default: serialize to stdout; `--write`: deep-merge into a rebar-owned `rebar.toml`
(existing keys win — hand edits are never clobbered).

The implementation is `src/rebar/_engine/rebar_reconciler/mapping_probe.py`:

- `ProbePort` — a Protocol exposing **only getters** (`issue_types`, `statuses`, `priorities`,
  `issue_link_types`, `createmeta_*`, `search_issues`, `transitions`). There is deliberately no
  create/transition/delete method — read-only is the whole point, so the probe can never mutate
  the instance the way `check-access` can.
- `build_probe(...)` — a **module-attribute** factory that constructs the default port over a
  real `jira.JIRA` client built from resolved Data Center settings. It is a module attribute so
  tests can `monkeypatch.setattr(mapping_probe, "build_probe", ...)` to inject an offline fake;
  the CLI handler obtains its port through this attribute for that patch to take effect.
- `build_mapping_layer(port, project_key)` — the **pure** builder that calls the port's read
  methods and returns `{"projects": {<KEY>: {<layer>}}}`.

The probe is **fail-soft** on every optional external getter: a Jira instance that 403s or omits
an attribute yields an empty axis plus an honest `_notes` entry, never a fabricated value or a
crash. A wholly-empty core vocabulary (no issue types **and** no statuses) is surfaced as a
`ProbeError` rather than emitted as a misleading empty suggestion.

> **`hierarchyLevel` is Cloud-only.** The `hierarchy` axis is derived from each issue type's
> `hierarchyLevel` attribute, which **Jira Data Center does not report** — every type lacks it.
> On Data Center the probe therefore **drops `hierarchy`** from the emitted layer (rather than
> fabricating ranks) and records an honest note explaining the omission; add ranks by hand if
> the project has a type hierarchy. Likewise `statuses` is the **global** status set (Jira
> exposes no per-project status list publicly), and `transitions_best_effort` is **partial**
> (only transitions out of each sampled issue's current status) — emitted under a clearly
> advisory key and never used to derive `status_map`.

## The reuse seam for future targets

The provider-neutral core (`mapping_config.py`: `MappingLayer` / `MappingConfig` / `Capability`,
`load_mapping_config`, `resolve_for_project`, `validate`) is the **reuse seam for future
targets** — Linear, GitLab, GitHub Issues. Because the core carries no vendor literal and takes
the built-in vocabulary and `Capability` as injected arguments, a new adapter reuses the entire
merge, resolution, and validation machinery by supplying its own built-in layer and capability
descriptor. Direct support for those targets is **out of scope now**; the seam exists so it can
be added without re-implementing the mapping engine.

## Migration and rollback

The mapping seam is **additive and backward-compatible**:

- **An absent `[tool.rebar.mapping]` / `[mapping]` section reproduces today's hardcoded behavior
  exactly.** Every `effective_*` resolver equals its built-in literal verbatim when no config is
  present, and the completeness gate never fires (the built-in type map is complete).
- **There is no store migration.** The feature is a config overlay and adds no event type, no
  schema change, and no rewrite of stored data. `.bridge_state/projects.json` is untouched (it
  is an orthogonal surface — see below).
- **Rollback = delete the section.** Removing the `[mapping]` block from the config file returns
  the reconciler to its built-in behavior with no further action.

`[mapping]` is orthogonal to `.bridge_state/projects.json`: the latter binds a store to a Jira
project **identity** (*which* project this store syncs to), while `[mapping.projects.<KEY>]`
declares that key's **vocabulary/semantics** (*how* local values translate). They share a Jira
project KEY as their join but neither subsumes the other.

## Portability

Every mapping behavior is exercised by **in-process unit tests with injected transports** — no
live Jira instance and no specific CI provider is required to validate the seam. The parsing,
three-layer merge, wholesale vocabulary replacement, fail-closed validation, capability gating,
and each `effective_*` resolver are all pure/config-only and tested in process. The probe's
tests inject an **offline fake `ProbePort`** by monkeypatching `mapping_probe.build_probe`, so
the whole builder runs with no live Jira and no CI credential.

The **only** live touchpoint is the `rebar bridge suggest-mapping` probe, and it is a manual,
operator-invoked convenience: it degrades to an offline emit (fail-soft axes plus honest notes)
and is **never a CI gate**. This satisfies the plan-review `project.portability` criterion —
no mapping behavior depends on a specific CI system or a live network target.
