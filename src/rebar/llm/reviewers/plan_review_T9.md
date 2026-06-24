---
schema_version: 1
title: Shared-state lifecycle [overlay]
description: Plan-review overlay-sharedstate criterion T9 (1-TURN). The rubric the
  Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-sharedstate
---
OVERLAY — apply when the plan introduces or mutates shared/global state (a cache, singleton, config key, shared file/record, or a stateful lifecycle); else PASS not-applicable. Check the full CREATE / UPDATE / CONSUME / RETIRE lifecycle: (a) who creates the state and when? (b) update concurrency / ownership clear? (c) consumers enumerated and tolerant of its absence/staleness? (d) is there a RETIRE/cleanup path, or does it leak/accumulate? SEVERITY: shared state with no defined ownership or no retirement path = MAJOR. ANTI-FP: not-applicable for purely local/stateless changes. ALSO assess CONCURRENCY SAFETY (distinct from lifecycle completeness): is shared/mutable state mutated atomically (lock/CAS/transaction, no check-then-act TOCTOU), and is the operation idempotent under retry / at-least-once delivery? A fully-specified lifecycle can still have a race.
