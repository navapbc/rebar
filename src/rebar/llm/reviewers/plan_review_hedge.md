---
schema_version: 1
title: Hedged-requirement provenance
description: Plan-review criterion `hedge` (1-TURN). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json. A cheap provenance signal that feeds
  Pass-2's committed_work_relies_on_unbacked_claim sub-answer. See ADR 0033 (exec:1-TURN,
  not exec:DET).
execution_mode: single_turn
category: plan-review-criterion
dimension: codebase-grounding
---
Scan the plan for HEDGED requirements or design premises — a committed element stated with a hedge
that signals unverified inference rather than established fact. Hedge frames: "probably",
"we assume", "should return", "presumably", "I think", "likely", "seems to", "as far as I know",
"in theory". Surface a finding when a COMMITTED element (an acceptance criterion, a task, an edit,
or a scope decision) rests on such a hedged assertion with no verification and no fallback — Pass-2
then grades its substance via `committed_work_relies_on_unbacked_claim` (a real dependence on an
unbacked claim upholds the finding; a hedge on a non-committed aside dissolves it).

Judge SUBSTANCE, not word-presence: a hedge on already-verified prose, on a premise the plan
explicitly flags as an assumption to test, or on a non-load-bearing aside is not a finding. When
no session log is linked, a hedged requirement is exactly the provenance-lite signal to route to
the riskiest-assumption coach move.

AC-CLAUSE SUPPRESSION (dedup vs E6.no_hedges — rubric-level, no pipeline change): if the hedge
sits inside an ACCEPTANCE-CRITERION clause where it stands in for a proving command ("should work",
"probably passes"), that is E6's `no_hedges` territory — mark THIS criterion not-applicable and do
NOT report it; E6 owns and reports that case. This criterion targets hedged requirements and design
premises OUTSIDE the AC proving-command surface, so the two never double-report the same hedge.

PASS (emit no finding) when the plan's committed elements rest only on verified premises or on
premises it explicitly flags as assumptions to validate.
