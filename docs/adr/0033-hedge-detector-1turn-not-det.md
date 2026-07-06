# ADR 0033 — The hedged-requirement detector is a 1-TURN finder criterion, not exec:DET

**Status:** Accepted (epic cite-stone-sea — DSO plan-review gap adoption / WS2 — catty-noun-lyre)
**Date:** 2026-07-06

## Context

DSO gap-report G-7(b) asks for a cheap signal on **hedged requirements** — a committed element
that rests on a hedged assertion ("probably", "we assume", "should return") rather than an
established fact. The natural first instinct is a deterministic detector (`exec:DET`): hedges are
lexical, and the DET floor is the cheapest tier.

Three verified facts about rebar's architecture rule `exec:DET` out for this signal:

- **The DET engine scans code, not plan prose.** `det_invariants.py`'s `engine_b.scan`
  (≈ line 112) operates on code-file ASTs / file-glob patterns. A plan description is prose, not a
  code file, so a DET check has nothing to scan for a hedged *requirement*.
- **DET criteria are stripped before the prose pass.** `orchestrator.route_criteria`
  (`orchestrator.py:332`) removes `exec:DET` criteria before `pass1_chunk` — the prose finder
  never sees them.
- **DET results bypass Pass-2 grading.** `partition_findings` stamps DET results with
  `validity=1.0`, so a DET "hedge" finding would be a hard, ungraded assertion — the opposite of
  what a soft provenance signal wants.

A hedge signal, by contrast, is exactly the kind of finding that Pass-2 should *grade*: whether the
hedge is load-bearing (a committed element truly depends on the unbacked claim) or incidental.

## Decision

Implement the hedge detector as a **1-TURN finder criterion** (`hedge`, matching E6's tier), **not
`exec:DET`**. It flows through the existing `pass1_chunk → verifier → pass3` pipeline with **zero
new infrastructure**, and — crucially — lets Pass-2 grade the hedge's substance via the WS1
sub-answer `committed_work_relies_on_unbacked_claim`. A hedge on a genuinely committed, unbacked
element upholds; a hedge on a non-load-bearing aside dissolves under grading.

**Dedup with E6 is rubric-level, not pipeline-level.** There is no location-based finding dedup
(`mint_finding_id` hashes finding-text + criteria, not location), so the `hedge` prompt carries a
rubric rule: a hedge inside an acceptance-criterion proving-command clause is E6's `no_hedges`
territory — mark it not-applicable and let E6 report it. No orchestrator change.

## Consequences

- The `hedge` criterion is registered like any other LLM criterion: `CANONICAL_LLM` + a
  `criteria_routing.json` entry (`exec: 1-TURN`, advisory) + the `plan_review_hedge.md` rubric.
- No DET-floor change, no new pass, no `partition_findings` change — the signal rides the existing
  prose pipeline and is graded, not asserted.
- If a future need arises for a *code-diff* hedge check (e.g. over committed code, not plan prose),
  that is a separate `exec:DET` question on the code-review side and does not change this decision.
