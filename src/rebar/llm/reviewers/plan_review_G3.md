---
schema_version: 1
title: Child coverage [agent, container]
description: Plan-review container criterion G3 (AGENT). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: container
---
CONTAINER-only (has_children): does the union of children cover the parent's acceptance criteria? 4-bucket audit per criterion (fully / partially / uncovered / structural) + a coverage map; an uncovered parent criterion is a finding. ANTI-FP: a criterion covered-by-definition by a named consumer counts.

READ THE ROSTER'S ACCEPTANCE CRITERIA. The complete sibling roster you are given lists every child as `- <id>: <title>` with THAT CHILD'S ACCEPTANCE CRITERIA INDENTED beneath it. Those indented items are the evidence for the absence test: before flagging a parent criterion as uncovered, check it against the roster's indented criteria for EVERY sibling — not just the child in this pairing — and flag it only if none of them delivers it. DECISION RULE: a parent criterion is UNCOVERED when no sibling's acceptance criteria in the roster deliver its observable outcome under the three-part standard below. A sibling whose roster block shows only its `- <id>: <title>` line has no parseable criteria, which is NOT evidence that it covers the parent criterion and NOT evidence that it fails to — treat that sibling as indeterminate for the absence test and say so in the finding rather than suppressing it. Do not treat a title alone as coverage.

THREE-PART COVERAGE STANDARD — a child covers a parent criterion only when ALL hold: (1) SAME OBSERVABLE OUTCOME (not a related one, not a precursor); (2) scope MATCHING-OR-EXCEEDING (no narrowing of conditions, users, data shapes, or environments); (3) measurable IN THE SAME TERMS. When in doubt, classify partial. THREE AC-CONTRADICTION PATTERNS a coverage map alone cannot see (each is a finding — the plan is structurally guaranteed to fail the completion verifier): bypass-annotation (a child plans to annotate/exclude items from the parent's metric instead of resolving them — 'the AC says zero matches, the DD annotates exceptions'); scope-narrowing (a child covers a narrower condition set than the parent criterion); partial-without-remainder (a child covers part and does not name the uncovered remainder).
