---
schema_version: 1
title: Assumption/premise verification [agent]
description: Plan-review codebase-grounding criterion E4 (AGENT). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
Scan the plan for assertions about the codebase ('X already exists', 'Y does Z', hedges/confident-assertions) and FORCE a Grep/Read probe per assertion; cached/training knowledge is not a substitute. Fail-closed on absent evidence (unverifiable assertion = gap). ANTI-FP: read the named implementation file before flagging a contract-doc-only claim.
