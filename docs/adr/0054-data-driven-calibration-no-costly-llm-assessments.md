# ADR 0054 — Calibrate from the field corpus; reject high-cost LLM-based assessments

**Status:** Accepted (story doggish-nonorganic-tsetsefly — divergence-kind grading / plan-v4)
**Date:** 2026-07-28
**Relation:** NARROWS ADR 0036 §"A/B gate". ADR 0036's permissive-rollout invariant,
version-tagging discipline, and no-pooling-across-versions rules are UNCHANGED and still binding.

## Context

ADR 0036 established the right instinct — never flip a threshold on judgement alone — but left
the verification instrument open-ended. In practice that has been read as licence to commission
purpose-built LLM work to justify a calibration: bespoke eval suites, labeled-set construction,
and bulk re-review sweeps that re-run the reviewer over many tickets to see how a candidate value
would have scored. Those plans have repeatedly turned out to be the most expensive part of a
calibration, and in several cases more expensive than the change they were gating.

The cost is avoidable, because **the gate already pays for this data during normal use.** Every
`review-plan` run persists a `REVIEW_RESULT` sidecar carrying, per finding, the full verification
payload: the binary sub-answers, every severity axis, `validity`, `impact`, `priority`, the
resolved `block_threshold`, `blocking_enabled`, and the decision with its reason. Measured on the
rebar store at the time of writing:

- **2,396** review runs carry metrics; **62,326 LLM calls** and **~298 hours** of model time
  already spent — a mean of 26 calls / 7.5 minutes per run.
- **51,964** verified findings are persisted with severity attributes; **18,085** on the
  then-current `plan-v3` impact model.

That is a large, version-tagged, in-the-wild labeled corpus that cost nothing extra to obtain.
The decisive property is that Pass-3 is **deterministic and pure**: `impact_*` and `pass3_decide`
are functions of the persisted verification payload. Any change to an impact model or a threshold
can therefore be **re-scored offline over the existing corpus with zero LLM calls** — the
counterfactual is computable, not something that must be re-elicited from a model.

Commissioning fresh LLM assessments to answer a question the persisted corpus already answers
buys no additional confidence. It is strictly worse on three axes: it costs money and hours, it
introduces sampling and prompt-construction bias absent from field data, and a hand-built labeled
set is smaller and less representative than the field corpus by orders of magnitude.

## Decision

### 1. The field corpus is the calibration instrument

Calibration MUST be driven by the version-tagged `REVIEW_RESULT` sidecars the gate produces during
normal use. A calibration proposal is expected to report, for the segment matching the current
`IMPACT_MODEL_VERSION`: per-criterion fire rate, validity distribution, priority percentiles, the
population that would change decision under the candidate value, and the **run-level** verdict
flip rate (how many PASS runs become BLOCK) — not only the finding-level count.

### 2. High-cost LLM-based assessments are REJECTED as calibration instruments

The following MUST NOT be required, planned, or commissioned in order to justify a calibration:

- **Purpose-built LLM eval suites** built for a single calibration question.
- **Bulk re-review sweeps** — re-running `review-plan` / `review-code` across a corpus of tickets
  to observe how a candidate value would have scored. This is the most expensive form and the
  least necessary, because Pass-3 is deterministic over already-persisted verification payloads.
- **New hand-labeled sets** commissioned as a precondition, when the field corpus already covers
  the question.

This is a rejection on **cost-effectiveness**, not on rigor: each is being declined because a
zero-marginal-cost method answers the same question at greater scale. It does not license
*unmeasured* changes — see §3, which remains mandatory.

The rejection is scoped to calibration. It says nothing about evals used for their own purposes
(prompt development, regression pinning of model behavior, capability checks). The existing
`ab_impact_model.py` gate over the checked-in `code_review_impact_labels.jsonl` fixture is
retained — it runs offline against a committed file at no LLM cost, and is an instance of §3(a),
not of the rejected category.

### 3. Two permitted A/B forms, both at zero marginal LLM cost

A calibration change is justified by **either** (either alone is sufficient; both is better):

**(a) Corpus replay.** Re-score the existing version-matched sidecar corpus under the candidate
value and report the decision deltas. Because Pass-3 is pure, this is exact for anything
downstream of the verification payload — thresholds, `blocking_enabled` flips, impact-model
aggregation changes, floor values.

> **Known limit, stated plainly.** Replay cannot model a change that alters what the *verifier
> emits* — e.g. a new closed grade set on an axis, which changes the Pass-2 output vocabulary
> itself. For those, replay yields a **bound**, not a prediction: re-score the corpus under each
> possible grade assignment to bracket the blast radius (best case / worst case), and treat the
> pessimistic bound as the number to reason about. Do not present a bound as a measurement.
> A change of that kind is then confirmed by (b), not by commissioning a re-review sweep.

**(b) Dogfood and observe.** Ship the candidate value and observe the gate in normal use, on the
work the team was going to do anyway. The observation is the sidecars that accumulate afterwards,
read with the same lenses as (a). This is the ONLY sanctioned way to validate a change replay can
only bound, and it is what "A/B" means here — sequential field observation, not a commissioned
parallel run.

Dogfooding a change that can newly BLOCK carries a real risk: a bad value obstructs the team's own
work. Two mitigations are required. First, prefer landing such a change with a **pressure-release
grade** — a sub-grade that scores below every blocking threshold — so the blunt version of the
change is never the one in the field (the `underspecified_oracle` precedent from calibration 3,
and `incomplete_enumeration` in this story). Second, `--force` on `claim` remains the operator
escape hatch, and a spike in `--force` usage is itself a calibration signal that the value is
wrong.

### 4. Version bumps are expensive; batch changes into one

An `IMPACT_MODEL_VERSION` bump closes the current cohort — ADR 0036's no-pooling rule means the
accumulated corpus for the old version cannot be replayed against the new model's scores. Bumping
is therefore the genuinely costly act in this process, not the analysis. **Batch related
impact-model changes into a single bump** rather than spending a cohort per change, and state in
the bump commit which changes are riding on it.

## Consequences

- Calibration becomes cheap enough to do routinely, which is the point: the previous cost
  structure discouraged the measurement ADR 0036 wanted.
- The burden of proof shifts to whoever proposes a *costly* method: they must show the field
  corpus cannot answer the question.
- A distinct failure mode is now possible and must be watched: the corpus can only tell you about
  findings the reviewer **surfaced**. It is silent on defects the reviewer never raised at all
  (false negatives outside the corpus). Field bug reports and post-hoc "the gate should have
  caught this" cases — like the G6 finding motivating this story — remain the only source of that
  signal, and are the legitimate trigger for a calibration rather than its instrument.
- Grade-set changes are structurally under-measurable before landing (§3a limit). The mitigation
  is the pressure-release grade, and the standing expectation that the first post-landing
  calibration re-examines the new grades' distribution.
