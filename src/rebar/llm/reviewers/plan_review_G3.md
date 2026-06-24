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
