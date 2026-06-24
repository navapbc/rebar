---
schema_version: 1
title: UX non-happy-path [overlay]
description: Plan-review overlay-ux criterion T6 (1-TURN). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-ux
---
OVERLAY — apply only if the plan introduces a user-facing interaction surface; else PASS not-applicable. Checks: (a) criticality — are the highest-stakes interactions named? (b) non_happy_path — validation/timeout/empty/partial-data/error states handled, not just the happy path? (c) flow_entry_exit — entry plus both success and abandon exit points covered? SEVERITY: a new interactive flow with only the happy path = MAJOR. ANTI-FP: not-applicable for backend/infra/data work.
