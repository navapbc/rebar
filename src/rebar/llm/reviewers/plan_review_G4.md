---
schema_version: 1
title: Child consistency [agent, container]
description: Plan-review container criterion G4 (AGENT). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: container
---
CONTAINER-only (has_children): check the 7 cross-child interaction modes — implicit shared state, conflicting assumptions, dependency gap, scope overlap, ordering violation, consumer impact, residual references. Each detected mode is a finding. ANTI-FP: high-confidence only; benign-reading filter.
