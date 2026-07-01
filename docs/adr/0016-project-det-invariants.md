# ADR 0016: Project DET-invariants — a data-driven detector→criterion consumer + a plan-time DET phase

- **Status:** Accepted
- **Context:** Epic *Project-supplied review criteria + project-invariant compliance
  (unified cross-gate registry)* (`3156`), story *DET-invariant scan consumer: data-driven +
  per-criterion `fail_mode`, exposed to plan-review* (`7f0d`). This is the follow-on to ADR 0015
  (which opened the plan-review vocabulary via a `.rebar/criteria_routing.json` overlay but left
  DET criteria LLM-only): it generalizes the hardcoded security-detector consumer into a
  data-driven one and lets an activated `exec: "DET"` project criterion run — as a grounding
  **detector**, not an LLM prompt — in both the code-review and plan-review gates.

## Context

Two hardcodings blocked a project from supplying a DET (pattern-rule) invariant:

1. **The code-review consumer was security-specific.** `code_review/detectors.py` filtered the
   detector registry by a literal `rebar.builtin.security.` prefix and mapped each detector to one
   of exactly two criteria (`secret-detection` / `high-critical-security`) via a hardcoded
   `_criterion_for`. An abstain always fail-CLOSED (blocked). There was no seam for a project's own
   detector, and no way to say "this invariant should fail *open*."
2. **Plan-review had no DET descriptor / phase.** `plan_review/registry._descriptor_from_prompt`
   ALWAYS resolved a prompt-library file, so a prompt-less DET criterion would fail to load; the
   static P1–P9 floor was frozen and closed; and `exec_tier` has no `DET` arm, so a DET criterion
   would misroute into the LLM batch (1-TURN) and reach `pass1_chunk`.

**Binding posture (inherited from epics `5fd2` / `3156`):** coach-not-block, advisory-by-default,
**fail-open**, reuse existing machinery — no plugin system, no new DSL. The one deliberate
exception is the security class, which stays fail-CLOSED (a coverage gap on secrets must block).

## Decision

### 1. Data-driven detector→criterion routing + per-criterion `fail_mode`

Each `exec: "DET"` routing entry may carry a `detector` **selector** (`{"id": …}` for an exact
match, `{"id_prefix": …}` for a prefix class) and a `fail_mode` (`"open"` | `"closed"`, default
`"open"`). `code_review/registry.det_criteria()` reads them; `criterion_for_detector(detector_id,
det_map)` resolves a detector to its criterion with **exact-id-wins-over-prefix** precedence — so
the gitleaks sentinel routes to `secret-detection` while every other `rebar.builtin.security.*`
routes to `high-critical-security`, reproducing the retired `_criterion_for` exactly. The packaged
security criteria declare their selectors + `fail_mode: "closed"` in `criteria_routing.json`, so
behaviour is byte-identical; the consumer just reads it from data now.

`detectors.run_detectors` (renamed from `run_security_detectors`, which remains a thin deprecated
alias asserted equivalent by a test) filters + buckets by these selectors. `apply_failclosed`
iterates `det_criteria()` (not the hardcoded pair) and reads each `fail_mode`: a **MATCH** blocks
per `blocking_enabled` (unchanged); an **ABSTAIN** blocks per `blocking_enabled` **only when
`fail_mode == "closed"`** — a `fail_mode: "open"` criterion records the abstain in coverage
(`fail-open-abstain`) but never blocks on absence. That is the generalization: project invariants
default to fail-OPEN; the security class stays fail-CLOSED.

### 2. A prompt-less `exec:DET` descriptor branch in plan-review

`_descriptor_from_prompt` resolves the routing entry FIRST; when `exec == "DET"` it builds the
descriptor WITHOUT calling `get_prompt` (a DET criterion is a detector, not an LLM rubric). The
`scenario` is the detector's rule `message` (resolved from the detector registry via the routing
`detector` selector), falling back to the criterion `name`/id when the detector suite is absent —
so a DET descriptor never depends on the detector tooling being installed. `_validate_routing_entry`
gains a `fail_mode ∈ {open, closed}` check (only when the key is present). This makes
`load_criteria` succeed on an activated project DET criterion that ships no `.rebar/prompts/…` file.

### 3. DET criteria are excluded from the LLM batch (read `exec` directly)

`orchestrator.route_criteria` skips any descriptor with `exec == "DET"`, and
`workflow_ops.plan_review_assemble_criteria` filters the `effective` inclusion vocabulary to
non-DET ids. **Both read `exec` DIRECTLY** — NOT via `exec_tier`, which has no `DET` arm and would
misroute a DET criterion to `1-TURN`. A DET criterion therefore never reaches `pass1_chunk`; it
owns no `include_<ID>` batch slot.

### 4. A two-phase `run_det_floor` — static P1–P9 + a dynamic project-DET phase

`det_floor.run_det_floor` runs the frozen static floor (`DET_CHECKS` = P1–P9) unchanged, then
appends `det_invariants.run_project_det_checks(ctx)`. `DET_CHECKS` stays static; the second phase
is the OPEN half. Each phase is **fail-open per check** (a raising check → an `abstain` DetResult,
logged); the whole project-DET phase is additionally wrapped so it degrades to nothing on any
error. **A repo with no activated `exec:DET` project criterion adds zero results**, so the static
floor is byte-identical to before.

### Plan-time DET scoping (a plan has no diff)

A detector describes a code smell, but at plan time there is no diff — so `run_project_det_checks`
SCOPES a match to the ticket's declared `file_impact`:

- a match on a **declared** file → a `fail` DetResult, blocking per the criterion's
  `default_posture` (the plan will carry the violation forward);
- a match on an **undeclared** file / with **no `file_impact`** → **advisory** (`fail`,
  non-blocking) — it cannot be attributed to this plan, so it is coaching, never a block;
- a **diff-inherent** smell (nothing the detector can locate at plan time) → **N/A** (a `pass`);
- an **abstain** (tool unavailable) → an `abstain` DetResult, blocking only when
  `fail_mode == "closed"` (mirrors the code-review consumer).

Because these flow through the existing `det_blocking_findings` / `det_advisory_findings` /
`det_coverage` helpers (which key on `DetResult.id`/`.name`), a project DET result surfaces in the
verdict exactly like a P1–P9 result — no new plumbing.

## Consequences

- A project can supply a DET invariant (a grounding detector + a routing entry) that runs in BOTH
  gates, choosing its own `fail_mode` (fail-open coaching vs fail-closed block) and posture.
- **Expand-contract:** the packaged security criteria are unchanged (their selectors +
  `fail_mode: "closed"` reproduce the prior hardcoded behaviour); every new signature defaults to
  the prior behaviour when no overlay/DET criterion is present. **Rollback** = delete the overlay
  (or revert the additive routing keys).
- Deferred to sibling stories (per ADR 0015): the per-criterion eval runner + calibration view, the
  editor live-preview authoring, the attestation-invalidation port, and the cross-gate unification
  into a shared `rebar.llm.criteria` layer.
