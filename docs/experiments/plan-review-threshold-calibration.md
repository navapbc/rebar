# Plan-review threshold calibration (first dogfood-data pass)

Story `3d3d-da49-c3f3-4298`. The plan-review gate shipped advisory-by-default with uniform
thresholds (`block_threshold=0.95`, `default_posture=advisory` for all 32 LLM criteria) so it
could not block during calibration. This is the first calibration of per-criterion blocking
defaults from the dogfooded `REVIEW_RESULT` sidecar corpus.

## What we record (the calibration substrate)

Every `review-plan` run emits a reducer-ignored `REVIEW_RESULT` sidecar (`sidecar.py`),
joinable offline by `ticket_id` + finding `id`. Per finding: `criteria[]`, `tier` (DET/LLM),
`decision` (block/advisory/dropped/indeterminate), `validity`, `impact`, `priority`
(= validity × impact), the 7 verifier binary sub-answers + the `cited_reference_accurate`
veto, and a stable content-hash `id`. Per event: `material_fingerprint` (a hash of
description/AC/file-impact/decomposition — a change between consecutive reviews = a genuine plan
revision), `coverage.routing` (which criteria ran — the fire-rate denominator), and per-pass
`metrics`.

A follow-on enrichment (this story) adds a reword-tolerant `norm_id` + the finding `location` to
the sidecar — **observability-only, the surfaced library/MCP/CLI verdict is unchanged** — so the
revision signal becomes cleanly joinable across re-reviews going forward.

## The two signals

1. **Verifier-refutation (dense).** Use the continuous `validity` distribution + `P(dropped)`
   + `P(indeterminate)`, NOT the literal `dropped` outcome alone (only ~1% of findings — far too
   sparse to calibrate per-criterion). Mean validity cleanly separates the false-positive-prone
   criteria (G1G2 0.35 / 41% indeterminate; T6 0.31 / 40%; T5b, E5, E6, F4) from the
   high-confidence ones (T10 0.88, T3 0.82, T1 0.80).
2. **Voluntary revision-response.** A `material_fingerprint` change between consecutive reviews =
   a revision. **Caveat:** exact finding-`id` survival is confounded — the Pass-1 finder rewords
   findings every run, so it reads ~100% "resolved" for every LLM criterion vs 13% for the one
   deterministic criterion (P6). So attribute revisions at **criterion-load-delta** granularity
   (did the per-criterion finding count drop after the revision?), and the `norm_id` enrichment
   matures it for future passes.

## Methodology → the decision

The block decision is `priority = validity × impact ≥ threshold` gated by `default_posture`. So a
criterion is worth flipping to blocking only if it (a) **produces** high-priority findings (high
validity × impact) and (b) **drives revisions** when it fires (the actionability/false-positive
test). Crossing those over the corpus, with an interactive review of the borderline/low-sample
criteria (some were candidates to block regardless of thin local data):

- **Flip to blocking @ `0.70`** (precision-first — only a verifier-confirmed, high-impact finding
  blocks): **G6, COH, T5e, E2, G5, F1, T4** (the dual-signal set).
- **Keep advisory:** the FP-prone set (G1G2, T6, T5b, E5, E6, F4) and the confident-but-ignored
  set (T3, T10, T8 — high validity but agents don't revise, so blocking = friction).
- **No policy blockers** for the low-sample criteria (data-driven only, by user decision).

## Validation (Step 7)

Re-ran `review-plan` under the new thresholds on a 20-ticket random sample drawn from tickets that
historically had high finding counts or overlay triggers: **7 BLOCK / 10 PASS / 3 INDETERMINATE**.
Every blocking finding had validity ≥ 0.857 and impact ≥ 0.75 (precise, not noisy), catching
genuine defects (internal AC/scope contradictions, destructive shared-branch removals,
architecturally-incompatible approaches, unresolved decisions deferred to the executor). Threshold
sensitivity showed `0.70` on a stable plateau (identical block set at `0.75` and `0.80`).

The 3 INDETERMINATE verdicts were traced to a **pre-existing bug** (`59bc`): the agentic Pass-2
verifier exhausted its step budget and the failure was misclassified as `llm_unavailable`,
discarding the Pass-1 findings and falsely blocking the claim. Fixed under `59bc` (budget scales
with finding count; verify failures preserve findings and fail open unless a finding is on a
blocking criterion; per-step usage is now recorded).

## Reproduce

```
python docs/experiments/calibrate_plan_review_thresholds.py    # reads .tickets-tracker/ sidecars
```

Emits the per-criterion table (n, fire-rate, mean validity, P(dropped), P(indeterminate),
criterion-load-delta revision-response, priority percentiles, worst verifier sub-question) and an
automated precision-first proposal. Recalibrate on a cadence as the sidecar corpus grows; condition
aggregates on `model` (the finder runs the Haiku→Sonnet→Opus ladder; the verifier defaults to
Sonnet), since priority distributions are model-dependent.
