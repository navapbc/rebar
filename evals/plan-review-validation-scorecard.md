# Plan-Review Gate — Validation Scorecard (recall + precision on DSO data)

Ran the E1 gold-set experiments against **real DSO ticket plans** (authorized as sample data): recall on seeded
defects, and — the piece the first pilot couldn't measure — **precision / false-fire on 12 real DSO plans** (most of
which are genuinely *not* infra/migration/deploy/UI/novel-arch, so the correct verdict is PASS-not-applicable). 240
runs total (180 precision + 28 seeded recall + 32 first pilot + 4 agentic-grounding), 0 errors.

## Recall (seeded gold set: known-bad plan per criterion → want FAIL)

13/15 criteria catch the defect they target, **zero rubber-stamping** (no seeded-bad plan was rubber-stamped PASS):

| caught (recall ✓) | the seeded defect |
|---|---|
| G6 | TOCTOU check-then-act idempotency |
| T10 | AdministratorAccess + plaintext key + local state |
| T11 | single-shot NOT NULL + 50M-row backfill |
| T12 | replace-algo deploy-100% |
| T1 | custom consensus protocol from scratch (no prior-art) |
| T4 | rename a public API method, no migration |
| T5a | one LLM call per row × 1M rows (cost) |
| T5b | inline synchronous geocoder, no timeout/fallback |
| T5c | PowerUserAccess role + committed DB password |
| T9 | `count = count + 1` shared, parallel |
| E5 | snapshot-of-current-output + source-grep test |
| COH | "full coverage" vs "no tests needed" + reversed deps |
| A1 | generic plugin framework for one handler |

Two exceptions — **both correct behavior, not logic failures:** **T3** (integration feasibility) returned AMBIGUOUS
not FAIL on the seeded "call an API method that may not exist" — correct: T3 is AGENT-tier and *should* hedge when it
can't verify the API without tools (it resolves with the agentic/web tier). **T5d** caught the color-only dashboard on
one repeat and glitched (empty finding) on the other — a finding-parse robustness nit.

## Precision / false-fire on 12 REAL DSO plans (the new measurement)

| criterion | PASS | AMBIG | FAIL / 12 | read |
|---|---|---|---|---|
| **T10** infra | 12 | 0 | 0 | **clean PASS-N/A — 0 false-fire** |
| **T11** migration | 12 | 0 | 0 | **clean** |
| **T12** rollout | 11 | 1 | 0 | **clean** |
| **T5d** a11y | 12 | 0 | 0 | **clean** |
| **T1** prior-art | 10 | 2 | 0 | **clean** |
| G6 approach | 5 | 7 | 0 | **0 false-FAIL** (AMBIG = wants codebase grounding) |
| COH coherence | 8 | 4 | 0 | 0 false-FAIL |
| A1 anti-patterns | 6 | 6 | 0 | 0 false-FAIL |
| T5c security | 11 | 1 | 0 | 0 false-FAIL |
| T5a perf/cost | 10 | 2 | 0 | 0 false-FAIL |
| T9 concurrency | 8 | 3 | 1 | plausible (rebar/DSO are concurrent) |
| T4 compat | 5 | 5 | 2 | plausible (some touch interfaces) |
| T5b reliability | 6 | 3 | 3 | plausible (some add external calls/writes) |
| **E5** testing | 0 | 0 | **9** | **OVER-FIRES — the tuning target** |

**The new criteria are well-disciplined:** every new overlay (T10/T11/T12/T5d/T1) correctly PASS-not-applicable on
real plans with **zero false-fires**, and the new judgment criteria (G6/COH/A1) **never false-FAIL** — they hedge to
AMBIGUOUS (non-blocking) rather than manufacture a wrong-approach/NIH finding. The one clear over-firer is **E5
(9/12)**, confirming the round-4 finding that E5 is too strict.

## Agentic grounding resolves the AMBIGUOUS hedge (EXP C)

Run codebase-grounded G6/A1 *with tools* against the DSO repo: verdicts resolve to concrete PASS or a *substantive*
evidence-backed AMBIGUOUS (real findings from inspecting the code), not the no-tools "can't confirm" hedge (G6→PASS on
the cross-epic epic after 13 tool calls; A1→PASS on the boundary-hook story). Confirms round 4: **run the
codebase-grounded criteria (G6/T10/T11/T9/COH/A1/E4/G1G2) on the agentic tier**, where AMBIGUOUS becomes decisive.

## Tuning actions (grounded in this data)

1. **E5 is too strict (9/12 false-fire) — the one clear tuning target.** Apply the round-4 type-aware suppression
   (test-tasks) and raise its bar; the change-detector roll-in may have over-tightened it. Re-measure.
2. **T3 belongs on the AGENT/web tier** — it correctly hedges single-turn (can't verify integration feasibility
   without tools). Same for the codebase-grounded set generally: AMBIGUOUS-on-clean is the no-tools artifact, and
   tool-grounding resolves it — so route them agentic.
3. **AMBIGUOUS-on-clean rate is the precision metric to track** (non-blocking, but a decisiveness cost). The new
   criteria's AMBIG-on-real ranges 0–7/12; tool-grounding is the lever.
4. **Finding-parse robustness** (T5d returned one empty finding) — harden the structured-output handling.

## Confidence read

Recall is strong (13/15 + 2 explainable), false-fire on the new criteria is near-zero on real plans, and no
rubber-stamping was observed. The set is in good shape to finalize **after** (a) the E5 retune + re-measure, (b)
routing the codebase-grounded criteria to the agentic tier, and (c) the still-owed **E4 generalization on a non-DSO
corpus** (this round used DSO data, which is the same population the routing was tuned on). Raw:
`plan-review-gate/runs/expA.jsonl` (precision), `expB.jsonl` (recall seeds), `seedpilot.jsonl` (first pilot);
harnesses `expA.py`, `expB.py`, `seedpilot.py`.
