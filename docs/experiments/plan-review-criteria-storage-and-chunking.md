# Plan-Review Gate — Criteria Storage, Chunking Strategy & Grounded Defaults (Round 3)

Turns the experimental results into **concrete, config-overridable defaults** for epic `5fd2-a7c2-0aec-48fa`:
how criteria are *stored* (so grouping scales and stays fresh), how the *chunker* decides chunk size from
model + ticket size + exec tier, and how the *single-turn vs agentic* split changes the strategy. Supersedes the
chunking/budget sections of `plan-review-chunking-budget.md` and `plan-review-model-tradeoff.md` where they
conflict; those remain the source for the underlying measurements.

Two new experiments back this (Sonnet 4.6, plan-text inputs + real codebase tools, ≈ $5):
- **EXP1 — substantive single-turn (240 calls):** re-ran chunking with the **fully-specified** criteria (real
  DSO checklists, `criteria_v2.json` — ~4× the text of round-1's one-liners) **and prompt caching** on.
- **EXP2 — agentic tier (17 runs):** ran the three tool-using criteria (E4, G1G2, A1) as **real agent loops** with
  Grep/Read/Glob over the rebar and DSO repos.

## What the first two rounds got wrong (corrections)

1. **My experiments tested a thin, single-turn-only subset.** The real v4 §5 registry spans four execution
   tiers (below); I had tested ~12 single-turn criteria with bare-bones one-line definitions. The full criteria
   set is larger and **tiered by execution model**, which is the dominant factor — not chunk size.
2. **"Coarse chunks are much cheaper" was a no-caching artifact.** With the plan cached (read at 0.1×), the cost
   gap between chunk sizes collapses from ~2.6× to ~1.55× (EXP1). Fine chunking is **not** expensive once caching
   is on — so within the single-turn tier you should pick chunk size for *reliability*, not cost.
3. **Substance does not lower the reliable chunk size.** Fully-specified criteria are *more* consistent at coarse
   N than the one-liners (Jaccard N=4: 0.61 vs 0.53; N=12: 0.65 vs 0.59) — explicit checklists reduce reviewer
   variance. Round-1/2 chunk-size guidance is safe (mildly conservative).

## The primary axis is the EXECUTION TIER, not chunk size

| tier | what it is | cost | chunkable? |
|---|---|---|---|
| **DET** (P1–P7) | deterministic code (LSP/ast-grep/syft/scc) | ~free | N/A — runs as parallel code, no LLM |
| **1-TURN** (F1,F4,E2,E5,G5,T5a–e) | one LLM call applying a checklist | ~$0.005–0.008/criterion (cached) | **yes — this is what the chunk-size knob governs** |
| **2-STEP** (E1,E3,E6) | two sequential LLM calls (map→check, restate→compare) | ~2× a 1-turn criterion | partially — chunk the passes, keep the 2 steps ordered |
| **AGENT** (E4,G1G2,A1,T1,T6,T7,T8) | a tool-using agent loop (Grep/Read/Glob/web) | **~$0.12/criterion, ~50s, ~15 tool calls (EXP2)** | **no — one agent per criterion** |

**EXP2 measured the agent tier directly:** E4/G1G2/A1 each ran ~15 tool calls over ~9 iterations in ~40–55s at
**~$0.12/run — ≈85× a single-turn criterion** — and *only with tools* did they produce grounded verdicts (e.g.
G1G2 flipped the dogfood epic's `"solidlsp (already in repo)"` claim from AMBIGUOUS to **FAIL** after a glob found
zero matches). **You cannot batch agentic criteria into one turn.** They are dispatched one-agent-each (parallel
across agents), and because each is ~85× a single-turn check they must be **gated by proportionate scrutiny**
(router/complexity), not run on every plan.

→ **Chunking only applies within the single-turn tier.** The chunker partitions by exec tier first.

## Storage model — facet-tagged criteria, generic chunker (scales & stays fresh)

Extend the criterion descriptor (reusing rebar.llm's `catalog.json`) with declarative fields the chunker reads —
**no hand-maintained chunk lists**:

```jsonc
{
  "id": "E5",
  "name": "Testing-plan completeness",
  "exec": "1-TURN",                 // DET | 1-TURN | 2-STEP | AGENT  — primary partition key
  "facet": "testing",              // the CONCERN it examines — the advisory grouping key
  "context_needs": "plan-text",    // plan-text | plan+children | codebase | web — provisions tools/agent
  "routing": "leaf",               // base | leaf | container | overlay
  "trigger": null,                 // for overlays: the router signal (e.g. "new I/O path")
  "checklist": [ ... ],            // binary checks (from the DSO-grounded specs)
  "block_policy": "advisory"       // advisory (default) | blocking-with-override
}
```

**Facets are the concern/context, not arbitrary groups** (your instinct was right). Derived from what each
criterion examines, so they stay meaningful as criteria are added/tuned:

| facet | criteria | why grouped |
|---|---|---|
| `ac-text-quality` | F1, E2, E6 | all reason over the acceptance-criteria text |
| `scope-intent` | F4, E3, G5 | scope, intent fidelity, sizing |
| `coherence` | E1 | cross-section consistency |
| `testing` | E5 | test plan |
| `codebase-grounding` | E4, G1G2, A1 | (AGENT tier) need the live repo |
| `overlay-*` | T5a perf, T5b reliability, T5c security, T5e maintainability, … | each its own concern |

**The chunker is generic and grouping is advisory:** (1) filter to applicable criteria (routing + overlay
triggers); (2) bucket by `exec` tier; (3) within the single-turn bucket, **greedily pack same-`facet` criteria
together** up to `effective_chunk_size`, spilling across facets only to fill a chunk; (4) dispatch each AGENT
criterion as its own loop; (5) run DET as code. Adding or retuning a criterion only requires declaring its
`exec` + `facet` — the chunker re-packs automatically at whatever chunk size the config sets. This is why
groupings are **advisory not prescriptive**: the same facet tags produce coherent chunks at *any* size.

*Why coherent packing matters (round-2 EXP B):* a fixed coherent partition let Sonnet hold Jaccard 0.70 at N=6 —
the reliability random chunking only reached at N=2. Same-facet packing is the mechanism; it is free here because
facets are already stored.

## Grounded defaults — chunk size by model × ticket size (single-turn tier)

Config-overridable. `base_chunk` from the per-model reliable ceiling (rounds 1–3); `size_factor` shrinks chunks
on big plans (more context consumed by the plan, more attention dilution):

```
base_chunk   = { opus: 12 (≈ all single-turn criteria, one call),
                 sonnet: 6  (with coherent facet packing; 3 without),
                 haiku/local: 3 }
size_factor  = { trivial: 1.0, moderate: 1.0,
                 large | epic | has_children: 0.5 }      # measured via AC count / children / desc length
effective    = clamp( round(base_chunk * size_factor), 2, n_singleturn_applicable )
```

| model | trivial/moderate | large / epic / has_children |
|---|---|---|
| **Opus 4.8** | all in 1 call (~12) | 6 per call (2 calls) |
| **Sonnet 4.6** (coherent packing) | 6 per call | 3 per call |
| **Haiku / local** | 3 per call | 2 per call |

Rationale: Opus's consistency is flat in N (round 2) so it packs everything in one cached call; Sonnet degrades
past ~3 *unless* coherently grouped, where 6 holds; both models destabilize at the *full* set on the largest
plans, so `size_factor` halves there. **Model choice** (round 2, now caching-adjusted): a cached fine-chunk
Sonnet review ($0.07–0.09) is roughly cost-competitive with one Opus pass ($0.10); prefer **Opus for max
consistency + big-plan stability**, **Sonnet+cache+coherent-chunks for the cost floor**.

## Grounded budget (per tier; generous, config-overridable)

| tier | per-criterion | a full review |
|---|---|---|
| DET | ~free | ~free (parallel code) |
| single-turn (cached) | $0.005–0.008 | ~$0.06–0.10 for all single-turn criteria |
| agentic | **~$0.12 + ~50s** each | dominated by how many fire |

**Per-plan cap (generous):** `cap = $0.15 (DET+single-turn base) + n_agentic_fired × $0.25 + headroom`. A small
task firing 0–1 agentic criteria ≈ $0.15–0.40; a heavy epic firing ~6 agentic + overlays ≈ $1.5–2.0 and a few
minutes wall-clock. **Enable prompt caching** (system + plan as the cached prefix) — it makes fine chunking cheap
and roughly halves repeat-review cost. INDETERMINATE on cap-hit: **shed the lowest-priority *agentic/overlay*
criteria first** (they're 85× the cost), never the cheap single-turn/DET base; record "review incomplete".

## Recommended edits to epic 5fd2 / plan-of-record §5–§6

1. **§5 registry:** add `exec`, `facet`, `context_needs` to the criterion descriptor schema (above). Store the
   DSO-grounded checklists (`data/dso-criteria-specs.md`) as each criterion's `checklist`, not one-liners.
2. **§6 chunking:** replace the single "chunk size" knob with the **exec-tier-aware** model: DET as code; the
   `base_chunk × size_factor` formula for the single-turn tier; one-agent-per-criterion for the AGENT tier gated
   by proportionate scrutiny. Default prompt caching on. Groupings are the stored `facet` tags (advisory).
3. **Budget:** adopt the per-tier model + the per-plan cap formula; shed-agentic-first on INDETERMINATE.
4. **Criteria count:** we evaluate **more than 12** — the single-turn set is ~13 (F1,F4,E1,E2,E3,E5,E6,G5 +
   T5a/b/c/e overlays) plus the AGENT set (E4,G1G2,A1,+T6/T7/T8/T1) plus DET P1–P7. The 12 in round 1 were a
   single-turn subset; this round adds the overlays, the agentic tier, and the DSO-grounded substance.

## Caveats

DSO is one creation style; single-turn runs are plan-text-only (the agentic runs ARE codebase-grounded but only
on 2 repos); no ground-truth labels (consistency is the trustworthy axis); Sonnet-only for EXP1 (Opus's caching
benefit within a single one-call review is nil — it's one call — so caching narrows but doesn't erase its
per-review premium); the `size_factor` 0.5 and `base_chunk` numbers are calibration starting points to re-derive
per client from live REVIEW_RESULT data. Agentic cost (~$0.12) is at ~15 tool calls / 10-iter cap — a tighter
loop or Haiku sub-agent would lower it.

Data: `data/exp1_substance.jsonl`, `data/exp2_agentic.jsonl`, `data/criteria_v2.json`,
`data/dso-criteria-specs.md`; harnesses `data/exp1_substance.py`, `data/exp2_agentic.py`; analysis
`data/analyze4.py`.
