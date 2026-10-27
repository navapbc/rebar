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

## Calibration 3 (plan-v2 segmented replay) — 2026-07-15

The first calibration run **segmented to the plan-v2 impact model** per ADR 0036 (task
`relishable-ammonitic-hoverfly`). Calibrations 1–2 were adjudicated against the retired
mean-impact formula; plan-v2 (severity-first MAX + the 0.85 hard-override floor,
`review_kernel.decide.impact_plan`) shipped the same day as calibration 2 and had never
been replayed. The script now reports the skipped remainder (this change), so the
segment is auditable:

```
python docs/experiments/calibrate_plan_review_thresholds.py --impact-model-version plan-v2
corpus: 1016 sidecars / 264 tickets / 23877 findings
skipped remainder: 1052 sidecars (different_version=217, untagged=835, unparseable=0)
```

**Headline results.** (1) NO criterion reaches BLOCK-ELIGIBLE under the precision-first
rule (needs rev_rr ≥ 0.6, validity ≥ 0.55, indet ≤ 0.15): the corpus-best revision
response is G5 at 0.565; the currently-blocking criteria sit at rev_rr 0.21–0.40 with
validity 0.47–0.62. (2) p90 surviving priority is EXACTLY 0.85 for nearly every
criterion (p75 too, for E6/F1) — the hard-override floor's signature saturating the
priority distribution. A companion floor-attribution pass over the segment's 2271
`decision==block` findings (recomputing `impact_plan` from persisted
`verification.severity_attributes` with the override disabled) found **35.5% of all
blocks are floor-driven** (would not have blocked without the 0.85 override), and
`ac_unverifiable` accounts for 48.9% of the floor-driven set — a majority of a
classified 100-finding sample fired at grade=low with an *under-specified* (not
missing) oracle. The structural lever is therefore the floor, not the thresholds; the
floor-axis split is story `large-sleepful-needlefish`.

**Adjudication (pre-registered decision rule; keep unless the replay affirmatively
moves a criterion).**

| criterion | thr (cal-2) | replay (n, validity, rev_rr, p90) | class | decision |
|-----------|------------|------------------------------------|-------|----------|
| T5e | 0.60 blocking | 901, 0.391, 0.21, 0.27 | FP-PRONE | **DEMOTE → advisory 0.95** |
| T8  | 0.70 | 2715, 0.595, 0.21, 0.85 | ADVISORY-KEEP | KEEP 0.70 |
| G1G2| 0.70 | 2263, 0.614, 0.21, 0.85 | ADVISORY-KEEP | KEEP 0.70 |
| G6  | 0.60 | 2123, 0.581, 0.21, 0.85 | ADVISORY-KEEP | KEEP 0.60 |
| E4  | 0.75 | 1659, 0.618, 0.28, 0.85 | ADVISORY-KEEP | KEEP 0.75 |
| E2  | 0.60 | 1446, 0.524, 0.26, 0.80 | ADVISORY-KEEP | KEEP 0.60 (watch: weakest keeper validity) |
| T1  | 0.70 | 1283, 0.554, 0.28, 0.85 | ADVISORY-KEEP | KEEP 0.70 |
| F1  | 0.60 | 912, 0.555, 0.35, 0.85 | ADVISORY-KEEP | KEEP 0.60 |
| COH | 0.60 | 557, 0.576, 0.40, 0.75 | ADVISORY-KEEP | KEEP 0.60 |
| T4  | 0.70 | 449, 0.474, 0.32, 0.57 | ADVISORY-KEEP | KEEP 0.70 (0.024 above FP-PRONE line — re-examine in cal-4) |
| G5  | 0.60 | 97, 0.469, 0.565, 0.53 | ADVISORY-KEEP | KEEP 0.60 (best rev_rr in corpus) |

T5e demotion rationale: the verifier refutes 59% of its findings (highest drop rate of
any blocking criterion), validity 0.391 is far below the 0.45 FP-PRONE line, and its
surviving p90 priority (0.27) means a raise-to-0.90 alternative would be a de facto
demotion that still pays block-path cost while misrepresenting posture. Demotion keeps
the coaching signal. No promotions (nothing BLOCK-ELIGIBLE); T7/F4/ISF are FP-PRONE but
already advisory; T5d stays LOW-DATA (n=17).

**Interpretation caution (recorded for calibration 4).** rev_rr measures
criterion-load drop across material revisions and is structurally depressed in
BLOCK-loop regimes with serialized discovery (~87% of blocking findings are new each
round, epic d195 evidence) — the uniform ADVISORY-KEEP outcome must NOT be read as
"demote everything"; it says plan-v2's discriminating mechanism is the hard floor, and
threshold surgery cannot fix floor saturation. Re-run this replay after the
`ac_unverifiable` floor split lands and the post-split corpus accrues (suggested
trigger: ≥300 post-split plan-v2 sidecars).
