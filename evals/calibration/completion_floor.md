# Completion-floor calibration (epic 66ac / story 77cf)

The Pass-2 completion sub-call (`plan_review_completion`) classifies each finding on three axes
(attribution / containment / layer) so the Pass-3 completion floor can drop findings that merely
re-litigate already-**delivered** child work. Because the changed artifact is a **prompt**, this is
a **live** LLM run (per G-Eval: freeze the wording, calibrate to a gold set whose anchors are the
must-never-suppress cases), not a replayed fixture.

- **Gold set:** `tests/unit/gold_set_completion.py` — 30 labelled cases, 6 per anchor category,
  spanning the G3/G4, coherence, and overlay provenances.
- **Prompt under test:** `src/rebar/llm/reviewers/plan_review_completion.md`
  (`sha256:1778b6349c3f…`). **A later prompt revision changes this hash and RETRIGGERS calibration**
  before the floor may be flipped on.
- **Model:** `claude-opus-4-8`.
- **Reproduce:** `REBAR_MCP_ALLOW_LLM=1 python scripts/calibrate_completion_floor.py`
  (the script builds a synthetic partial epic, runs the real sub-call over the gold findings, and
  scores the model's answers — and the resulting floor decision — against the gold labels).

## Results

| Metric | Result |
|--------|--------|
| Containment agreement (model vs gold) | 24/30 = **80%** |
| Layer agreement (model vs gold) | 24/30 = **80%** |
| **Floor decision match (drop/keep vs gold)** | **30/30 = 100%** |
| **Cohen's κ (drop/keep)** | **1.0** |
| **Must-never-suppress violations** (a KEEP anchor wrongly dropped) | **0** |

Per-category floor-decision accuracy (correct / total):

| Category | Result | Meaning |
|----------|--------|---------|
| `DROP` (pure re-litigation of delivered plan text) | 6/6 | the only drop category — all correctly dropped |
| `DELIVERED_FUNC` (delivered mechanism/contract) | 6/6 | preserved |
| `SECURITY_CONTRACT` (T5c / T10) | 6/6 | preserved (preserve-set veto) |
| `CROSS_SIBLING` (spans an open sibling) | 6/6 | preserved |
| `FORCE_CLOSED` (unverified child) | 6/6 | preserved (not delivered-now) |

## Reading

- **The decision is what matters, and it is perfect here (κ = 1.0).** Per-axis agreement is 80% —
  the model occasionally labels a case's `containment`/`layer` differently from the gold — yet the
  **floor decision is 100% correct** and **zero must-never-suppress anchors were dropped**. This is
  by design: the floor drops only on the **conjunction** of (attribution ∈ delivered-set) ∧
  (containment = limited-to-closed) ∧ (layer = plan-semantics) ∧ (priority < floor) ∧ (not
  preserved). The six per-axis disagreements all fell on the **keep** side of that conjunction, so
  they never produced a false drop. The floor absorbs classifier noise into the safe direction.
- **The `delivered-set` guard carries the force-closed anchor.** Every `FORCE_CLOSED` case is kept
  regardless of the model's containment/layer answer, because its attributed child is not in the
  delivered manifest — "delivery is proven, not assumed" (ADR 0024). This is the anchor that
  motivated hardening the floor from "attribution ≠ none" to "attribution ∈ delivered-set."
- **The preserve-set veto carries security/contract.** `T5c`/`T10` findings are kept even when the
  model calls them delivered plan-semantics.

## Verdict

The floor's **decision** clears the G-Eval κ ≈ 0.8 bar (κ = 1.0) with **no must-never-suppress
violations** on this gold set — the evidence the operator needs before flipping
`verify.completion_floor_active` on. The 80% per-axis agreement is recorded as the headroom to watch:
a prompt revision that *lowered* containment/layer agreement enough to break the conjunction on a
real corpus would first show up here (hence the retrigger-on-prompt-hash rule).

## Monitoring (dogfooded in rebar's own project — story c366)

`verify.completion_floor_active = true` is set in this project's `rebar.toml` so the floor runs
live on re-fired epic/story-with-children plan-reviews. Because a suppression removes a finding
from the surfaced advisory list, it must remain **auditable** — a wrongly-suppressed finding has
to be recoverable. Every drop is persisted, not just logged:

- **Where drops land.** On any review where the floor drops one or more findings, each dropped
  finding is moved out of `advisory[]` into the plan-review **sidecar** `dropped[]` array, tagged
  `drop_reason="completion"` and carrying its full completion sub-answers (`completion`), so the
  reason for the drop is inspectable. The verdict `coverage` also records
  `completion_floored_finding_ids` (the dropped finding ids) and `completion_floored_criteria`
  (the affected criterion ids). A run that dropped nothing leaves the verdict byte-identical and
  writes no `completion`-tagged entries.
- **How to audit.** The sidecar for a review is written next to the plan-review attestation.
  To review recent completion suppressions, inspect a review's sidecar and filter
  `dropped[]` for `drop_reason == "completion"` (each entry carries the original finding text,
  its `criteria`, and the completion answers that justified the drop). The runtime also emits an
  `INFO` log line naming the floored finding ids whenever the floor drops on a run, so live
  suppressions are visible without opening the sidecar. If a legitimate finding was suppressed,
  the finding is fully reconstructable from the sidecar entry.
- **Rollback.** The floor is advisory-only and cannot change a PASS/BLOCK verdict, so a wrong
  suppression is never verdict-affecting and is always recoverable. To disable it entirely, set
  `verify.completion_floor_active = false` in `rebar.toml` — an immediate, cheap revert that needs
  no data cleanup (the flag simply goes inert and reviews return to byte-identical output).
