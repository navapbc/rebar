---
schema_version: 1
title: Criteria↔description coherence + terminology + duplicates
description: Plan-review coherence criterion E1 (2-STEP). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: coherence
---
Audit internal coherence of the requirement set (this is naturally a two-pass check: first map each criterion to the described work, then cross-check terminology and duplicates). Binary checks: (a) every acceptance criterion maps to something described in the plan body, and every described deliverable has a covering criterion (no orphan criteria, no uncovered work); (b) terminology is consistent — the same concept is named the same way throughout, and a criterion's verify step references the SAME entity its text names (e.g. 'cycle' vs 'review_cycle'); (c) no duplicate or near-duplicate requirements; (d) for migrations, criteria verify BOTH removal and replacement. SEVERITY: a deliverable with no covering criterion, or a criterion measuring nothing, is MAJOR; terminology drift / near-dup is MINOR. ANTI-FP: cite-or-omit — ground every finding in specific quoted plan text; prefer AMBIGUOUS over a hand-waved FAIL; consumers named in the spec are covered-by-definition. PASS if the set is coherent and non-redundant.
