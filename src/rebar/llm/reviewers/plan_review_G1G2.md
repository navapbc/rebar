---
schema_version: 1
title: Edit-set / scope accuracy [agent]
description: Plan-review codebase-grounding criterion G1G2 (AGENT). The rubric the
  Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
Verify (via Glob/Grep) that every file/symbol the plan names actually exists; enumerate consumers/callers OUTSIDE the artifact's dir that a change would require updating; flag hallucinated/missing edit targets and unenumerated consumers; classify behavioral hunks in/ambiguous/out-of-scope (CREATION=new behavior->out-of-scope). High blast-radius alone is not a fail if acknowledged. ANTI-FP: report only high-confidence; STOP if scope too vague. Any symbol created by a ticket this ticket depends_on (evaluated recursively) is treated as if it EXISTS and is NOT MISSING.
