---
schema_version: 1
title: Child coverage [agent, container]
description: Plan-review container criterion G3 (AGENT). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: container
---
CONTAINER-only (has_children): does the union of children cover the parent's acceptance/success criteria? 4-bucket audit per criterion (fully / partially / uncovered / structural) + a coverage map; an uncovered parent criterion is a finding. ANTI-FP: a criterion covered-by-definition by a named consumer counts.

THREE-PART COVERAGE STANDARD — a child covers a parent criterion only when ALL hold: (1) SAME OBSERVABLE OUTCOME (not a related one, not a precursor); (2) scope MATCHING-OR-EXCEEDING (no narrowing of conditions, users, data shapes, or environments); (3) measurable IN THE SAME TERMS. When in doubt, classify partial. THREE SC-CONTRADICTION PATTERNS a coverage map alone cannot see (each is a finding — the plan is structurally guaranteed to fail the completion verifier): bypass-annotation (a child plans to annotate/exclude items from the parent's metric instead of resolving them — 'SC says zero matches, the DD annotates exceptions'); scope-narrowing (a child covers a narrower condition set than the parent criterion); partial-without-remainder (a child covers part and does not name the uncovered remainder).
