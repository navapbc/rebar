# ADR 0036 — Impact-model permissive rollout + version-segmented calibration cadence

**Status:** Accepted (epic foliaged-merry-collie — review-gate impact redesign / story raptorial-galloping-dragon)
**Date:** 2026-07-08

## Context

The review-gate impact redesign replaced the kernel mean `impact` with two severity-first models —
`decide.impact_plan` (ADR-adjacent) and `decide.impact_code` (ADR 0035). Both change scoring
significantly. Flipping blocking thresholds down on day one — before we know how the new scores
distribute on real, in-the-wild reviews — risks either a flood of false blocks or a silent
regression. The plan-review gate's own rollout established the safe playbook: **ship permissive,
dogfood, accumulate a field corpus, and lower thresholds only when data + an A/B justify it.**

Two hazards make a naive "collect sidecars, then retune" loop unsafe:

1. **Corpus contamination.** Sidecars produced by the OLD formula and the NEW formula are not
   comparable. Pooling them hides the very shift we are trying to measure.
2. **No objective gate.** "The new model looks better" is not a decision criterion. We need a
   reproducible test that either justifies a threshold-down or refuses it.

## Decision

### Permissive rollout is an invariant, not a config change
The impact redesign introduced **no new hard block**: every impact-graded (LLM) criterion in both
routing configs is `default_posture: advisory` at `block_threshold` 0.95; code-review's only two
`blocking_enabled: true` entries are the pre-existing `exec: DET` security detectors. We LOCK this
with a test (`test_impact_model_versioning.py`) so a future edit cannot silently introduce an
impact-driven hard block — the rollout stays permissive by construction until a calibration
justifies otherwise.

> **Enablement note — 2026-07-12 (epic 9d50 / story b9c0): the first LLM criterion now blocks.**
> The calibration justification arrived. Story 9f25 derived the code-review block threshold from
> the deduped code-v2 finding corpus (161 findings): the priority distribution has a hard **0.60
> ceiling**, with the #518-class importlib code-execution security findings at the ~0.54 band.
> On that evidence, **`security` (an `exec: AGENT` criterion) is flipped to `blocking_enabled:
> true` at `block_threshold` 0.54** — the first LLM criterion permitted to block, a deliberate
> exception to the "only exec:DET blocks" invariant above. `test_impact_model_versioning.py`,
> `test_code_review_ws2.py`, and `test_code_review_deletion_impact.py` are updated to pin the new
> approved blocking set `{secret-detection, high-critical-security, security}`; every other LLM
> criterion stays advisory at 0.95, and any further addition must be a re-approved change. 9f25's
> 0.54 is provisional pending a held-out (non-circular) adjudication confirmation — see
> `docs/experiments/derive_code_review_threshold.md`.

### Version-tag every review artifact
Each `REVIEW_RESULT` sidecar stamps a top-level `impact_model_version` (`plan-v2` / `code-v2`) from a
module constant in the respective `sidecar.py`. The calibration replay segments on it and treats a
MISSING tag as "unknown / skip" — the same discipline as the per-finding `cohort` carrier. **Bump the
constant whenever the formula's shape changes**; that starts a fresh calibration cohort and the
segmented replay will never mix the old and new scores.

### The calibration cadence
1. **Ship permissive** (above) and let the gate run.
2. **Accumulate** version-tagged sidecars in the shared store (the data-capture child's `code_review`
   artifacts + plan-review's `REVIEW_RESULT` sidecars).
3. **Segmented replay** — `docs/experiments/calibrate_plan_review_thresholds.py --impact-model-version
   <v>` reports per-criterion fire-rate / validity / revision-response / priority percentiles for THIS
   version only.
4. **A/B gate** — `docs/experiments/ab_impact_model.py` scores the checked-in labeled set with the
   new model vs the baseline mean and EXITS NON-ZERO unless the new model's HIGH↔NIT separation
   strictly beats the baseline's. A threshold-down is proposed ONLY when this gate passes AND the
   segmented field corpus shows the separation holds at scale.
5. **Re-run per version bump** — each `IMPACT_MODEL_VERSION` change resets the cohort; do not carry a
   threshold tuned on an older version into a newer one without re-running steps 3–4.

## Consequences

- Threshold tuning becomes a data-gated, reproducible decision rather than a judgement call.
- The version tag is additive (existing sidecar readers ignore it); only the calibrator opts in.
- The A/B currently scores the code-review fixture (`code_review_impact_labels.jsonl`) — the only
  checked-in labeled set; plan-review's calibrator is driven off live sidecars via the segmented
  replay. A checked-in plan-review labeled set, if added later, plugs into the same A/B shape.
