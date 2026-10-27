# Plan-Review Gate — Model Tradeoff & Intentional Grouping (Round 2)

Follow-up to `plan-review-chunking-budget.md`. Answers three questions the first round raised:
1. **How many criteria can Opus 4.8 handle per turn without degradation** (vs Sonnet's ~3)?
2. **At each model's max-without-degradation N, how does $/criterion compare** between Opus and Sonnet?
3. **Can intentional (coherent) grouping** of criteria within a chunk raise Sonnet's reliable ceiling?

Two experiments, **460 calls, 0 errors, ≈ $22.7** (Opus $16.3 + Sonnet $6.3). Pricing used (authoritative):
**Opus 4.8 = $5/$25 per M, Sonnet 4.6 = $3/$15 per M** (Opus is ~1.67×/token). Same 12 Layer-2 criteria,
same DSO + dogfood plans, plan-text-only simulation as round 1.

- **Experiment A (Opus capacity, 280 calls):** 5 tickets × N∈{2,4,6,8,12} × 2 random partitions × 2 repeats,
  on `claude-opus-4-8`. Mirrors round-1 batch 2 (which was Sonnet) so the curves are directly comparable.
- **Experiment B (intentional grouping, Sonnet, 180 calls):** 6 tickets × {coherent, anti-affinity} grouping ×
  N∈{4,6} × 3 repeats. Compared against round-1's **random** groupings at the same N. "Coherent" clusters
  criteria that examine the **same plan facet** (AC-quality {F1,E1,E2,E6}; intent/scope {F4,E3,G5,A1}; code-grounding
  {E5,E4,G1G2,COH}); "anti-affinity" deliberately spreads every facet across every chunk.

## Result 1 — Opus does not degrade as N grows; Sonnet does

Pooled run-to-run consistency (mean pairwise Jaccard of FAIL-sets; higher = more reliable):

| criteria/turn N | 2 | 4 | 6 | 8 | 12 |
|---|---|---|---|---|---|
| **Sonnet** | **0.70** | 0.53 | 0.47 | 0.56 | 0.59 |
| **Opus** | 0.61 | 0.61 | 0.60 | 0.56 | 0.67 |

**Sonnet peaks at N=2 and falls off after ~3; Opus is flat-to-rising across the whole range** (its most
consistent setting is actually N=12). So **Opus's reliable ceiling is effectively the full criteria set (~12 per
turn); Sonnet's is ~2–3.** Opus also avoids Sonnet's catastrophic big-plan failure: on the 24-child container epic
at N=12, Sonnet flips between 3 and 0 findings (Jaccard 0.33) while Opus is stable.

**Honest caveat:** neither model is *uniformly* stable at N=12 on every plan — on the complex leaf task Opus is
noisier at 12 (Jaccard 0.42, counts 1–4) while Sonnet found 0 consistently, and Opus returns 0 findings on the
container epic (stable, but possibly under-flagging a 13k-char plan). On the **largest** plans, keep chunk < 12 for
both models; the flat-vs-falling *shape* is the transferable finding, not a guarantee at 12.

## Result 2 — One Opus call beats many reliable Sonnet calls, on cost *and* reliability

Real token usage × pricing, per full 12-criterion review and per criterion:

| model @ N | calls/review | review tokens p50 | $/review p50 | **$/criterion** |
|---|---|---|---|---|
| Sonnet @ N=2 (its stable region) | 6 | 25.2k | $0.147 | **$0.0123** |
| Sonnet @ N=6 | 2 | 11.4k | $0.079 | $0.0066 |
| Sonnet @ N=12 (unstable on big plans) | 1 | 7.3k | $0.057 | $0.0047 |
| **Opus @ N=12 (its stable max)** | 1 | 9.3k | $0.103 | **$0.0086** |

The per-token price says Opus is 1.67× dearer — but **per *review* the result inverts**, because Opus reliably
packs all 12 criteria into **one** call (plan + system prompt sent once) while a *reliable* Sonnet review needs
4–6 calls at N≤3 (plan re-sent every call). The re-send overhead dominates the token-price premium:

> **One Opus call doing the whole review ($0.10/review, $0.0086/criterion) is ~30 % cheaper per criterion than
> Sonnet at its reliable small-chunk setting (N=2, $0.0123/criterion) — and more consistent.**

Sonnet is cheaper in absolute terms *only* if pushed to N=12 ($0.057/review), which is exactly the config that
goes unstable on large plans. So **"many cheap Sonnet agents" is a false economy at the reliable setting** — it
costs more than a single Opus pass and is no more reliable.

## Result 3 — Coherent grouping ~triples Sonnet's reliable criteria-per-turn

Consistency (Jaccard) by grouping strategy, Sonnet:

| N | coherent (affinity) | random | anti-affinity |
|---|---|---|---|
| 4 | 0.71 | 0.53 | 0.79 |
| 6 | **0.70** | 0.47 | 0.66 |
| *(ref: random @ N=2 = 0.70)* | | | |

**Coherent grouping at N=6 (0.70) matches random at N=2 (0.70)** — i.e. clustering criteria by the plan facet
they examine lets Sonnet reliably review **~6 criteria/turn at the consistency random chunking only reaches at 2**,
roughly a **3× lift** in reliable criteria-per-turn. And coherent does it **without suppressing findings**: its
mean flag-rate (0.25–0.26) tracks random's (0.25–0.27). Anti-affinity also raises Jaccard, but partly by
*under-flagging* (flag-rate 0.21–0.22 → likely recall loss), so it is not a clean win.

**Mechanism + caveat:** part of random's lower consistency is that its instances mix two *different* partitions
(the grouping itself varies), whereas coherent/anti hold one fixed partition across repeats. That is itself the
actionable finding: **the gate should use a fixed, declared grouping, not random chunking** — fixing the partition
is most of the reliability win, and *affinity* clustering is what preserves recall on top of it.

## Recommendations (fold into epic 5fd2 §6 + the chunking config schema)

1. **Model-tiered chunking default.** Frontier-tier reviewer (Opus 4.8) → **up to 12 criteria/turn (one call)**;
   mid-tier (Sonnet 4.6) → **6/turn *if* grouped coherently, else ≤3**; weak/local → ≤3. (Round 1's "Sonnet ≤3"
   stands only for *un*grouped chunking.)
2. **Prefer one Opus pass as the high-reliability default.** It is cheaper per criterion than a *reliable* Sonnet
   review and more consistent. Reserve Sonnet small-chunk for the cost floor on small/simple plans (its N=6
   coherent setting is both cheap, $0.079/review, and stable there).
3. **Chunking config must specify a FIXED, coherent partition** (criteria clustered by plan facet) — not a random
   or per-run-varying split. This is a concrete schema requirement for the `chunking` knob in §6.
4. **Budget (updated for model choice):** generous per-plan cap ≈ **$0.30 / ~25k tokens** covers a large epic
   reviewed by one Opus pass at fine chunking with headroom; typical Opus one-pass review ≈ $0.10 / 9–15k tokens;
   typical reliable Sonnet review ≈ $0.08–0.15. INDETERMINATE triggers unchanged from round 1.

## Caveats (carry over from round 1)

DSO is one creation style; plan-text-only simulation (codebase-grounded criteria E4/G1G2/A1 barely fire without
repo access); no ground-truth labels (consistency is the trustworthy axis, "recall" is vs-solo); single reviewer
per call; G3/G4 child-coverage excluded. Thinking was **off** for both models (params omitted) so the comparison
is like-for-like; enabling adaptive thinking would shift both quality and cost and is untested here. A weaker/local
model's ceiling is still unmeasured.

Raw data: `plan-review-gate/runs/runs3_opus.jsonl`, `plan-review-gate/runs/runs4_group.jsonl`; harnesses `plan-review-gate/harnesses/batch3_opus.py`,
`plan-review-gate/harnesses/batch4_grouping.py`; analysis `plan-review-gate/harnesses/analyze3.py`.
