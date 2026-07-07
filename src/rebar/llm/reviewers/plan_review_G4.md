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

CONSUMER-ENUMERATION PRECURSOR: before analyzing the consumer-impact mode, first ENUMERATE all consumers of the modified system (a worklist), then analyze each — a recall→worklist pass over the existing consumer-impact mode so no consumer is silently missed. SCOPE-GAP corollary: an item 'out of scope' for one child may be 'in scope' for NONE — flag an owned-by-none gap.
