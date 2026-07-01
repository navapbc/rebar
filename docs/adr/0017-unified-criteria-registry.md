# ADR 0017: Unified criteria registry — one shared `rebar.llm.criteria` layer both gates delegate to

- **Status:** Accepted
- **Context:** Epic *Project-supplied review criteria + project-invariant compliance
  (unified cross-gate registry)* (`3156`), story *Cross-gate unification: one shared
  `rebar.llm.criteria` layer* (`5065`) — the capstone of the epic. ADR 0015 opened the
  **plan-review** vocabulary via a `.rebar/criteria_routing.json` overlay; ADR 0016 added the
  data-driven DET-invariant consumer + a plan-time DET phase. Both left the machinery
  DUPLICATED across the two gates (plan-review's `plan_review/registry.py` and code-review's
  `code_review/registry.py`) with the overlay support living only in plan-review. This ADR
  extracts the shared machinery into one layer and gives code-review overlay support.

## Context

Two gates had grown two copies of the same machinery, drifting:

1. **Two `threshold_for` resolvers.** Plan-review derived `blocking` from `default_posture ==
   "blocking"`; code-review read an explicit `blocking_enabled` flag. Same math (min
   threshold), divergent blocking derivation — an intentional divergence (see below) that was
   nonetheless expressed as two separate code copies that could silently drift.
2. **The descriptor builder + the overlay core were plan-review-only.** `_descriptor_from_prompt`
   (exec-tier polymorphic since ADR 0016's DET branch) and the whole
   `.rebar/criteria_routing.json` merge / activation / cache-isolation core (ADR 0015) were
   keyed to the literal `"plan_review"` gate. Code-review had **no** overlay support at all — a
   project could add plan-review criteria but not code-review criteria.

**Binding posture (inherited from epics `5fd2` / `3156`):** coach-not-block,
advisory-by-default, **fail-open**, reuse existing machinery — no plugin system, no new DSL.
**And, for this story specifically: DELEGATION, not rip-and-replace.** Both gates keep their
public functions; behaviour must be byte-identical for an overlay-absent repo (both gates' full
suites stay green as the proof).

## Decision

A new shared package `rebar.llm.criteria` HOSTS the machinery; each gate's registry keeps its
public functions, which now **delegate** to the shared layer with a `gate_key` / `gate`
argument. An overlay-absent repo is byte-identical to before.

### 1. `threshold_for(criteria, routing_map, *, gate)` — both conventions, side-by-side

The reconciled resolver lives in `criteria/model.py`. `block_threshold` = the MIN over the
criteria's thresholds (default `0.95`). `blocking` is **gate-dispatched** — the divergence is
PRESERVED, not collapsed:

- `gate == "plan_review"` → True iff any criterion has `default_posture == "blocking"` (the
  plan-review convention: the criterion's intended posture IS its runtime posture);
- `gate == "code_review"` → True iff any criterion has `blocking_enabled: true` (the
  code-review convention: an EXPLICIT enable flag, separate from the staged `default_posture` —
  the detector keys ship `default_posture: "blocking"` yet must run ADVISORY in v1, which only a
  separate flag expresses; WS5 flips exactly those two keys).

Plan-review's `orchestrator.pass3_over_findings` passes its descriptor map (`registry.by_id()`,
which carries `block_threshold` + `default_posture`) with `gate="plan_review"`; code-review's
`registry.threshold_for` passes its routing map with `gate="code_review"`. Both are byte-identical
to their prior private resolvers — a unit test asserts the SAME criterion (`default_posture:
blocking`, `blocking_enabled: false`) BLOCKS under `plan_review` but not `code_review`, and vice
versa, so the divergence can never be "fixed" away by accident.

### 2. `build_descriptor(cid, routing_entry, *, repo_root, prompt_getter)` — exec-tier polymorphic

The descriptor builder (generalizing plan-review's `_descriptor_from_prompt`, ADR 0016's DET
branch included) also lives in `criteria/model.py`. For `exec == "DET"` it builds a PROMPT-LESS
descriptor (the `scenario` is the detector's rule message, resolved from the detector registry
via the routing `detector` selector) — so an activated project DET criterion that ships no
`.rebar/prompts/…` file still loads. For every other tier it resolves the rubric via the
**injected `prompt_getter`** (plan-review passes a wrapper around its `get_prompt`; the seam lets
a future code-review LLM-criterion path pass its own, or `None` for DET-only use).

### 3. The overlay core, gate-parameterized (`criteria/overlay.py`)

The `.rebar/criteria_routing.json` merge / activation / cache-isolation logic (ADR 0015) is
generalized to take a `gate_key`. Each gate REGISTERS itself once at import
(`register_gate(gate_key, packaged_index=…, canonical=…)`, both providers callables so a test
monkeypatch of a gate's canonical set is honoured on a fresh overlay signature — mirroring how
ef7e read the module globals inside the cache). The merged-view lru_cache is keyed by
`(gate_key, repo_root, sha256(overlay-bytes))` — the exact per-repo content-signature isolation
from ef7e's G6 fix, now spanning both gates in one cache; `prompt_library._invalidate_caches`
clears it via `criteria.clear_caches()`.

Consequently **code-review gains overlay support**: `code_review/registry.effective_routing` /
`effective_criteria` read the overlay's `code_review` gate key from the SAME
`.rebar/criteria_routing.json`, so a project can add code-review criteria + re-tunes exactly as
it does for plan-review. `routing_index()` (packaged) stays intact for back-compat, and
`threshold_for` still defaults to the packaged map (a repo-aware caller may pass
`effective_routing`) so behaviour is byte-identical when no overlay is present.

### The shared `activate` list is gate-aware

The overlay's `activate` list is a single top-level list shared by both gates. A project
criterion defined for one gate's map but not the other legitimately appears there. So an
activated `project.` id with no routing entry in **this** gate is only a located error when it is
defined for **no** registered gate at all — otherwise it is simply not active for this gate
(each gate's effective vocabulary stays isolated). The "activate a criterion that exists nowhere
is a located error" contract is preserved.

## Consequences

- One implementation of the threshold resolver, the descriptor builder, and the overlay core —
  the two gates' registries are thin delegating façades. `plan_review/registry.py` shrank from
  ~670 to ~445 LOC; the shared layer is `criteria/{model,overlay}.py` (~190 / ~300 LOC).
- **Code-review gains project-supplied criteria** through the same `.rebar/criteria_routing.json`
  overlay, on its `code_review` gate key.
- **Expand-contract:** an overlay-absent repo behaves byte-identically to before for BOTH gates
  (the merged routing is `dict(packaged_index())`); both gates' full suites stay green as the
  proof. **Rollback** = delete the overlay (or revert the `rebar.llm.criteria` package and
  re-inline the two copies).
- The shared error is `criteria.CriteriaError`; plan-review re-exports it as `RegistryError`, so
  every existing `except`/`pytest.raises` against the gate error keeps working.
- Deferred (per ADR 0015): the per-criterion eval runner + calibration view, and the editor
  live-preview authoring, remain sibling stories.
