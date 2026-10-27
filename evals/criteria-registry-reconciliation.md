# Criteria-Registry Reconciliation — where "EXP" (and other criteria) got lost

Joe flagged that the round-4 "EXP" criterion should not have been novel. It wasn't: **EXP = T2 empirical probe**,
already in the design-of-record. Reconciling the full v4 §5 registry (session log `63cc-ec94-4792-464b`,
"DESIGN OF RECORD: plan-of-record-v4.md") against everything actually implemented across the experiment rounds
shows EXP/T2 was one of **several criteria that got dropped and never reconciled.**

## The authoritative registry (v4 §5) vs what was implemented

| group | criterion | status across experiments |
|---|---|---|
| DET | P1 readiness-shape | proxy via `rebar check_ac` only |
| DET | P2 resolution · P3 deps · P5 task-DAG · P6 AC/DD · P7 destructive | **not exercised** (code tier, separate) |
| DET | P4 oversize | proxy via `clarity_check` |
| L2 | F1, F4, E1, E2, E3, E5, E6 | single-turn ✓ |
| L2 | G1+G2, E4, A1 | agentic ✓ |
| L2 | G5 decomposition | used (from v3; not in v4 §5's explicit list — folded from P4/G3-G4) |
| L2 | **G3 child-coverage, G4 child-consistency** | **designed, never run** (container tier) |
| overlay | T1 prior-art | router only (never run as a review criterion) |
| overlay | **T2 empirical probe** | **★ reinvented as "EXP" in round 4 ★** |
| overlay | **T3 integration feasibility** | **LOST — never implemented or tested** |
| overlay | **T4 compat/destructiveness-as-explicit-choice** | **LOST — never implemented or tested** |
| overlay | T5a perf, T5b reliability, T5c security, T5e maintainability | single-turn ✓ |
| overlay | **T5d accessibility** | **dropped from the criteria set** (round-4 router keyword only) |
| overlay | T6 UX, T7 docs, T9 shared-state | router only |
| overlay | T8 LLM-antipatterns | agentic probe + router ✓ |
| cross-cut | **coherence pass** (cross-section contradictions) | **COH in round 1, then dropped** (folded into E1) |
| cross-cut | broad open-ended pass | run in the dogfood gate ✓ |
| cross-cut | benign-reading filter, anti-fabrication | in the SYSTEM prompt ✓ |

## Where it got lost (the trace)

1. **Round 1** — I compiled `criteria.json` as a 12-item *bare-bones "Layer-2 judgment subset"*
   (F1,F4,E1,E2,E3,E5,E6,G5,E4,G1G2,A1,COH) "to keep the experiment focused." That step **dropped all 13 triggered
   overlays (T1–T9), including T2.**
2. **Round 3** — `criteria_v2.json` was built *from the round-1 subset*, adding back only 4 overlays
   (T5a/b/c/e). T1,**T2**,T3,T4,T5d,T6,T7,T8,T9 stayed dropped; the dedicated **COH** coherence pass was dropped too.
3. **Round 4** — when the empirical-validation need surfaced, I **did not recognize it as the existing T2** and
   reinvented it as "EXP."

**Root cause:** there was never a canonical registry file derived from v4 §5. Each `criteria_vN` was built from the
*previous subset*, so once a criterion was dropped it never came back — and a real one (T2) got reinvented under a
new name. (The EXP *experiment* is still valid — it correctly validated the empirical-probe behavior; only the
*name* was wrong.)

## Fix

1. **Renamed EXP → T2** ("Empirical probe (red→green / spike) [overlay]"); the round-4 experimental result stands,
   now under its real id.
2. **Restored the dropped criteria** as descriptors in `criteria_v4.json`: **T3** integration feasibility, **T4**
   compat/destructiveness, **T5d** accessibility, and the dedicated **COH** cross-section coherence pass — grounded
   in the DSO catalog (feasibility-reviewer, compat/expand-contract, accessibility.md, coherence verdict-rubric).
3. **Completed the set**: `criteria_v5.json` now has **all 22** single-turn/overlay descriptors (the v4 fix only
   restored T2/T3/T4/T5d/COH — the coverage guard then revealed T1/T6/T7/T8/T9 were *still* missing, now added too),
   plus the 5 agent-tier criteria (G1G2/E4/A1/G3/G4) in a separate harness.
4. **Added a mechanical completeness guard** (`plan-review-gate/harnesses/check_registry_coverage.py`): it encodes the canonical v4 §5
   registry and **fails loudly** if any criteria file omits a criterion. Run against the old `criteria_v2.json` it
   lists all 10 dropped criteria — i.e. it would have caught this the day it happened. This is the missing checklist
   whose absence let the drop go unnoticed.

## Still owed (in the corrected set, but no experimental DATA yet)

These are in v4 §5 but have never been exercised — flagged so they aren't mistaken for "done":
All 22 single-turn/overlay criteria are now descriptors, but several have never been *run*: **T1/T2/T3/T4/T5d/T6/
T7/T8/T9** (T2/T8 were exercised; the rest are restored descriptors only), the **COH** coherence pass, **G3/G4**
container child-coverage (now runnable — the epic has children), and the **DET tier P2/P3/P5/P6/P7** (deterministic
code, separate from the LLM experiments). Recommended next: a validation pass over the never-run overlays +
container criteria so the whole registry has coverage data.

Data: `plan-review-gate/criteria/criteria_v5.json` (complete corrected set), `plan-review-gate/harnesses/check_registry_coverage.py` (the completeness guard),
`plan-review-gate/runs/reconcile.json` (registry-vs-implemented map).
