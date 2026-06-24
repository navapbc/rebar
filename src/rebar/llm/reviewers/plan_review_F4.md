---
schema_version: 1
title: User/problem present (value)
description: Plan-review scope-intent criterion F4 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
Check that the plan names WHO the work is for and WHAT problem they face, and that value is validatable. Binary checks: (a) the context names a specific user/stakeholder and the problem they have today; (b) the criteria collectively represent an observable improvement to that user or a measurable business outcome, not pure system internals; (c) it is NOT a bare technical task with no named beneficiary ('Refactor the service layer'); (d) at least one criterion carries a concrete validation mechanism (before/after workflow comparison, an operational metric target, dogfooding). SEVERITY: no named beneficiary AND no value-validation is MAJOR; missing only the validation mechanism is MINOR. ANTI-FP: an IMPLIED technical consumer counts for low-level/internal tasks (cleanup, dep upgrades, library internals) — do not flag those; backend work affecting latency/reliability scores normally via an operational signal. PASS if a beneficiary and the value are clear.
