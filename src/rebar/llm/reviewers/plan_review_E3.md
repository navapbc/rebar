---
schema_version: 1
title: Intent fidelity
description: Plan-review scope-intent criterion E3 (2-STEP). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
Judge whether the plan faithfully serves its stated title/goal (a blind-restate-then-compare check: first restate what the plan actually does, then compare to what the title promises). Binary checks: (a) the body's work matches the headline intent — no scope drift doing MORE or LESS than the title promises; (b) each non-deferred goal has a faithful, end-state-observable proof; (c) no step contradicts the stated intent; (d) where the plan changes existing behavior, it acknowledges callers that depend on the old behavior. SEVERITY: the plan builds something materially different from its stated intent = CRITICAL (agent will build the wrong thing); partial drift = MAJOR; minor mismatch = MINOR. ANTI-FP: mixed or absent signals → AMBIGUOUS, not a forced FAIL; 'no implementation found yet' is not itself intent-contradiction. PASS if the plan is faithful to its goal.
