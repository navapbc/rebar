# Plan-Review Gate — Grounding Experiment: Chunking & Budget Defaults

Grounds two not-yet-built defaults for the plan-review verification gate (epic
`5fd2-a7c2-0aec-48fa`, design-of-record in session log `63cc-ec94-4792-464b`):
**(1) how many Layer-2 criteria to batch per LLM call/turn** and **(2) the budget
posture** (token + latency envelope per review). It SIMULATES the gate: a Sonnet-4.6
reviewer applies a chunk of the Layer-2 advisory criteria to a real ticket's plan
(description + acceptance criteria) and returns per-criterion findings authored to the
§4 feedback spec. Inputs are real plans, READ-ONLY, from the DSO store (`~/digital-service-orchestra`)
plus our own epic 5fd2 as a dogfood target.

- **Model:** `claude-sonnet-4-6` (frontier reviewer; default temperature for natural repeat-variance).
- **Criteria pool:** the 12 Layer-2 judgment criteria (F1, F4, E1, E2, E3, E5, E6, G5, E4, G1G2, A1, COH);
  see `criteria.json`. Container-only child-coverage criteria (G3/G4) excluded (need fetching all children).
- **Two batches, 919 calls, 0 terminal errors, ≈ $25.71 total.**

## Method

**Batch 1 (475 calls, 100 review-cells).** 5 tickets × chunk sizes {1, 3, 6, 12=ALL} × 5 repeats.
A fixed partition per size. chunk-size-1 = per-criterion quality baseline. Reviewer blind to the variable.

**Batch 2 (444 calls).** 6 tickets (+a 2nd, smaller epic) × criteria-per-turn N ∈ {2,4,6,8,10,12} ×
2 **random** partitions × 2 repeats, + a 60-call solo (N=1) baseline for the new ticket.
Random groupings **decouple chunk size from which criteria are grouped together** (batch 1's confound).

Tickets (stratified by AC count / children / description length):
`trivial` (task, 2 AC) · `moderate` (task, 5 AC) · `complex_leaf` (task, 12 AC, ~10k chars) ·
`small_epic` (epic, 19 children) · `container_epic` (epic, 24 children, ~13k chars) ·
`dogfood_epic` = our 5fd2.

Metrics per (ticket × N): **recall** vs solo baseline (does batching drop criteria the per-criterion
review catches?), **specificity / over-flagging** vs solo, **consistency** (mean pairwise Jaccard of the
FAIL-criteria set + stdev of finding-count across repeats), and **token + wall-clock** cost.

## Result 1 — Chunking / criteria-per-turn

**Batching does NOT drop criteria.** Recall of solo-flagged (majority) criteria is **100 % at every N up
to 12.** Even all-12-in-one-call retains every finding the per-criterion baseline produced. This refutes the
design's central worry that coarse chunking dilutes recall — on this corpus it does not.

The cost of batching is **lower run-to-run consistency** and **more over-flagging vs solo**, and both appear by
**N≈4 and then plateau** (they do not keep worsening 4→12 in aggregate):

| metric (pooled across tickets) | N=2 | N=4 | N=6 | N=8 | N=10 | N=12 |
|---|---|---|---|---|---|---|
| recall of solo-positives | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| specificity vs solo (1−over-flag) | 0.89 | 0.75 | 0.74 | 0.80 | 0.75 | 0.77 |
| consistency (mean Jaccard) | **0.70** | 0.53 | 0.47 | 0.56 | 0.56 | 0.59 |
| finding-count stdev | **0.5** | 0.8 | 0.9 | 0.9 | 0.8 | 1.0 |

Two refinements:
- **Large/complex plans destabilize at high N.** `container_epic` Jaccard 1.0 at sizes 1/3/6 (batch 1) →
  **0.33–0.40 at N=10–12**, with whole reviews flipping to 0 findings; `complex_leaf` is bimodal; `dogfood_epic`
  size-12 stdev 1.6 (one run collapsed to 0). Small/moderate plans tolerate high N fine. So the all-at-once
  failure mode is **plan-size-dependent**.
- **Some "over-flagging" is legitimate context-dependence, not noise.** Cross-cutting criteria fire *more* when
  they can see sibling criteria/sections: **E5** (testing completeness) solo 0.03 → 0.33–0.50 batched; **COH**
  (coherence) solo 0.00 → up to 0.50. Reviewing these *in isolation under-fires them.* Argues against N=1 for
  cross-cutting criteria.

**→ Default: 4 criteria per turn** (= **3 parallel LLM calls** per 12-criterion review). The knee: specificity
& consistency have already plateaued, recall is intact, and it costs ~2× less than N=2. Going finer buys fidelity
at 2–4× tokens (reserve for high-stakes); going coarser saves little and risks instability on big plans.

| complexity tier | criteria/turn | turns/review |
|---|---|---|
| trivial / moderate | 6–8 | 2 |
| **default** | **4** | **3** |
| complex / has-children / epic | 2–3 | 4–6 |
| weak / local models (any tier) | ≤3 | 4+ |

**Never put all 12 in one call for an epic/large plan** (the only clear quality cliff observed). Frontier
Sonnet's stable ceiling for *big* plans is ~6; for small plans it is effectively 12.

## Result 2 — Budget posture

Full-review token cost (all 12 criteria over their chunks), and the cost of chunking finer:

| criteria/turn N | calls/review | review tokens p50 | p95 |
|---|---|---|---|
| 1 (finest) | 12 | 51k | up to **72k** (big epic) |
| 2 | 6 | 25k | 39k |
| **4 (default)** | 3 | **15k** | **21k** |
| 6 | 2 | 11k | 16k |
| 12 (all) | 1 | 7k | 10k |

Per-tier at the default (N=4), full-review tokens p95: trivial 8.6k · moderate 9.2k · small_epic 14k ·
dogfood_epic 18k · complex_leaf 18.5k · container_epic 22k. Latency (parallel wall-clock) p50 ≈ 34–60s,
worst single-call p95 ≈ 88s.

**Recommended generous budget cap: ~90k tokens + ~240s wall-clock per plan.** Rationale: 90k covers even the
worst case measured — a large epic reviewed at the *finest* N=1 chunking (p95 ~72k) — plus headroom, so the cap
never clips a legitimate review at the default or any finer setting. 240s covers a serial fallback of a deep epic
review. The *typical* envelope (default N=4, moderate ticket) is ~15–25k tokens / ~40–60s — track that for
telemetry; the cap is a backstop, not a regular gate.

**INDETERMINATE triggers:** (a) cap hit mid-review → checkpoint between chunks, shed lowest-priority remaining
criteria, emit INCOMPLETE/INDETERMINATE (never a silent PASS); at a 90k cap this essentially never fires on these
plans, which is the intended "generous" behavior. (b) A chunk erroring after retries — 0/919 here, so rare. (c)
A required sound tool unavailable (per design §3; fail-open at the criterion level, INDETERMINATE only if a
top-tier check can't run).

## Result 3 — Dogfood review of our epic 5fd2

Run against our own epic (batch 1, per-criterion baseline, 5/5 repeats), our criteria flag — consistently and
correctly:

- **G5 decomposition — MAJOR, conf 0.95, 5/5 FAIL:** "scoped as an epic but has NO child tickets … the 12 ACs
  span 8–10 distinct deliverables." **Exactly the predicted "decompose into children" finding.**
- **E5 testing plan — MAJOR, conf 0.92, 5/5:** "The plan has no testing plan … no unit tests for the Layer-1
  deterministic checks, no integration tests for the workflow."
- **E2 / E1 / E6 / F1 — MINOR, 4–5/5:** the chunking/budget/depth defaults are deferred to external experiment
  results rather than inlined → flagged as ambiguity / orphan-AC / end-state-not-self-contained. **This is the
  gap this very experiment closes** — folding the numbers above into the epic resolves these.
- **E4 assumptions — MINOR:** "solidlsp already in repo", "reuse rebar.llm catalog/contracts/runner" asserted;
  an executor should verify before relying.

The gate, run on its own design, produces the two correct structural critiques (decompose; add a test plan) plus
correctly notices the deferred-defaults hole. Strong dogfood signal.

## Caveats (honest)

- **DSO is one creation style** (strict typing, RED/GREEN TDD tasks, heavy shell) — *not* representative of the
  diverse client base. Absolute flag-rates will not transfer; the **shape** of the curves (recall-preserved,
  consistency-best-at-low-N, instability-at-high-N-on-big-plans) is the transferable finding.
- **This SIMULATES the gate** with a single Sonnet reviewer working **from plan text only.** Codebase-grounded
  criteria (E4, G1G2, A1) almost never fired (≈0.00 solo *and* batched) because the reviewer had no repo access;
  in production they run tool-grounded (AGENT exec) and their chunking economics are **untested here.**
- **No ground-truth labels.** "Recall" is recall-vs-solo-baseline; "noise" is over-flagging-vs-solo, not vs
  truth. Consistency is measured directly and is the most trustworthy axis.
- **Single model** (Sonnet 4.6). Weaker/local models will have lower stable-N ceilings — consistent with the
  design's "small models chunk finer."
- **Container child-coverage criteria (G3/G4) excluded** → epic reviews here under-test the container path.

## Recommended defaults (to fold into epic 5fd2 + plan-of-record §6/§12)

1. **Chunking default = 4 criteria / turn** (3 parallel calls/review); scale **2–3** for complex/has-children,
   **6–8** for trivial/moderate, **≤3** for weak models; **never all-12 for epic/large plans.**
2. **Budget cap (generous) = ~90k tokens + ~240s** per plan; typical envelope ~15–25k / 40–60s; INDETERMINATE on
   cap-hit (shed-lowest-priority), chunk error, or top-tier-tool-unavailable.
3. A **second validation round** is warranted before locking: (a) add non-DSO corpora (other creation styles);
   (b) run the codebase-grounded criteria with real repo access; (c) test a weaker model to locate its stable-N
   ceiling.

Raw data: `runs.jsonl` (batch 1), `runs2.jsonl` (batch 2); harnesses `harness.py`, `batch2.py`; analyses
`analyze.py`, `analyze2.py`; criteria `criteria.json` — under the experiment job tmp dir.
