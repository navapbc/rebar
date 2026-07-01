# ADR 0015: Project-supplied review criteria — a `.rebar/` routing overlay over one shared registry

- **Status:** Accepted
- **Context:** Epic *Project-supplied review criteria + project-invariant compliance
  (unified cross-gate registry)* (`3156`), story *Criteria registry: open the vocabulary +
  `.rebar/` routing overlay + activation + cache isolation (plan-review MVP root)* (`ef7e`).
  This ADR documents the MVP-root slice landed for the **plan-review** gate; the DET-invariant
  consumer, per-criterion eval runner, editor authoring, attestation-invalidation port, and the
  cross-gate unification into `rebar.llm.criteria` are follow-on stories under the same epic.

## Context

rebar's plan-review gate ships a **closed** set of criteria: the LLM vocabulary is the
hardcoded `CANONICAL_LLM` frozenset in `plan_review/registry.py`, and each criterion's routing
(exec tier / applies_at / block_threshold / default_posture / checklist) lives in the packaged
`criteria_routing.json`. A client project cannot add a criterion of its own — naming rules,
layering, banned APIs, architectural invariants — because there is no seam to open the
vocabulary, and the registry's caches (`load_criteria`, `_routing_index`) were process-global
(`@lru_cache(maxsize=1)`, no `repo_root` key), so even if a project criterion were injected it
would leak across repos in the long-lived MCP server.

**Binding posture (inherited from epic `5fd2`):** coach-not-block, advisory-by-default,
**fail-open**, polyglot / broad client base, **reuse existing machinery — no plugin system,
no new DSL, no new formats.**

## Decision

A project supplies criteria through a **`.rebar/criteria_routing.json` overlay** that reuses the
packaged routing schema, merged over the built-ins by a repo-keyed discovery seam. No project
code runs in the gate; an unreadable/absent overlay fails open to the packaged behaviour.

### Overlay schema (gate-keyed, namespaced, explicit activation)

```json
{
  "plan_review": { "<id>": { …routing… }, … },
  "code_review": { … },              // reserved for the code-review gate (later story)
  "activate":    ["project.<name>", …]
}
```

- **Namespace / collision (load-time, located errors):** a net-new criterion id MUST be
  `project.<name>`-prefixed. An **un-prefixed built-in id** is a *re-tune* (its routing is merged
  per-key over the packaged entry) or *disable* (a later story). A `project.`-prefixed id equal
  to a built-in, or a net-new id that is not `project.`-prefixed, is **rejected at load** — a
  project id can never rebind a built-in. A malformed overlay (bad JSON, wrong shape, invalid
  `exec`/`block_threshold`/`default_posture`) is a **located** `RegistryError`, never a silent
  skip.
- **Explicit activation:** a project criterion runs only if listed in `activate` — presence in
  the file is **not** activation. Built-ins are always active; listing one in `activate` is a
  no-op.

### The vocabulary seam

`effective_criteria(repo_root)` = `CANONICAL_LLM` ∪ activated project ids, and
`effective_routing(repo_root)` = the packaged index merged with the overlay's `plan_review` map.
These are routed through **every** plan-review vocabulary site — `load_criteria`,
`check_registry_coverage`, `route_criteria`, and (transitively) the workflow Pass-1 assemble op
— so an activated project criterion is loaded, routed, and surfaced exactly like a built-in. A
project criterion's rubric is a prompt-library file resolved verbatim by `get_prompt` at
`.rebar/prompts/plan-review-project.<name>.md`; a missing/malformed rubric fails loud from
`load_criteria`.

### Pass-1 fan-out is runner-side, NOT a schema change

The v3 `batch` step schema (`workflow.v3.schema.json`, `additionalProperties:false` on
`$defs/batch`) is **immutable and version-pinned**, and the gate validates its own document at
run time — so a new `batch` field is impossible without a v4 schema + migration shim (out of
scope). Built-in criteria keep fanning out through the **static** `criteria:` list in
`gates/plan-review.yaml`. Activated **project** criteria are fanned in by the rebar-specific
`ProductionBatchRunner`: after `assemble_context`, it takes the `project.`-prefixed subset of
`route_criteria(ctx)` (already past the same `applies()`/overlay filter as the built-ins) and
appends it to the finder's tier-split set. The gate YAML and the immutable schema are untouched.
The assemble op still emits sanitized `include_project_<name>` booleans (`.`→`_`, a valid
workflow output key) as the coverage/routing record.

### Cache isolation (the G6 fix)

The packaged `_routing_index()` stays `@lru_cache`d (immutable per binary). The overlay-merged
views (`effective_routing`, `load_criteria`) are `@lru_cache(maxsize=128)` keyed by
`(repo_root, sha256(overlay-bytes))` — a **content signature**, not mtime (mtime granularity is
coarse/flaky). Editing an overlay yields a new key ⇒ a fresh compute (no stale routing); the
per-repo key means a long-lived MCP server serving many repos never leaks one repo's routing
into another (proved by a RED cross-repo-leak test); LRU eviction bounds growth.
`prompt_library._invalidate_caches()` clears all three so a same-signature in-process authoring
write is visible without a restart.

### Attestation invalidation (story 08af)

A plan-review **claim-gate attestation** must go stale when the overlay it was reviewed under
changes — otherwise a project could activate/edit/disable a criterion and keep claiming against a
review that never saw it. Three moving parts make the gate overlay-aware:

- **`registry_version(repo_root)` hashes the overlay.** The registry-version stamp bound into
  every signed manifest is now overlay-aware: with `repo_root` given it hashes the repo's
  **effective** routing (`effective_routing`) plus the overlay's activated-project ids and
  disabled-built-in set, so activating / re-tuning / disabling any criterion changes the stamp.
  It stays **expand-contract**: with `repo_root=None`, or a repo with **no overlay**, the basis is
  **byte-identical** to the historical packaged stamp (the `activated`/`disabled` dimensions are
  added only when non-empty), so attestations signed before this change stay valid — zero churn.
- **The `stale-regver` claim-gate check.** `compute_validity`'s plan-review branch compares the
  manifest's signed `regver:` against the current `registry_version(repo_root)`; a mismatch — or a
  **missing** `regver:` line (expand-contract: every production plan-review manifest carries one) —
  is `{valid: false, verdict: "stale-regver"}`, forcing a fresh `review-plan` before the claim.
- **Built-in `disabled: true`.** An overlay `plan_review` entry for an **un-prefixed built-in** id
  may carry `"disabled": true` (rejected on a `project.` id — turn a project criterion off by
  omitting it from `activate`). A disabled built-in is removed from `effective_criteria` (never
  loaded/run) while its routing entry stays resolvable in `effective_routing`; `disabled_builtins
  (repo_root)` returns the sorted disabled ids. The signed manifest records them on an additive
  `disabled_builtins: <a,b>` line (absent — byte-identical to a pre-08af manifest — when nothing is
  disabled), parsed back by `manifest_disabled_builtins`.

## Consequences

- A project can add plan-review criteria (LLM prompts today; DET pattern-rules in a follow-on)
  through one overlay, with the project owning activation and (later) blocking/threshold policy.
- The change is **expand-contract**: every new/changed signature defaults `repo_root=None` →
  `config.repo_root()`, so an overlay-absent repo behaves byte-identically to before. **Rollback**
  = delete `.rebar/criteria_routing.json` (or revert the additive params).
- A **CI parity gate** (`python -m rebar.llm.plan_review.registry validate-routing`) keeps the
  packaged `criteria_routing.json` in sync with `CANONICAL_LLM` (no missing/orphan/malformed
  entry) — the analog of the `reviewers/index.json` drift gate, adapted to hand-authored routing.
- Deferred to sibling stories: the per-criterion eval runner + calibration view; the editor
  live-preview + authoring; and the cross-gate unification into a shared `rebar.llm.criteria`
  layer (gated on `b744`). (DET-invariant scan consumer + per-criterion `fail_mode` landed in
  `7f0d`; the attestation-invalidation port landed in `08af` — see above.)
