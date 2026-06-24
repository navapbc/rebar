---
schema_version: 1
title: Testing-plan completeness (retuned v7)
description: Plan-review testing criterion E5 (1-TURN). The rubric the Pass-1 finder
  applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: testing
---
Assess whether the plan makes the work testable by construction. FIRST apply the applicability gate: fire ONLY if the plan INTRODUCES new logic/behavior that is testable. If the change is internal/mechanical (refactor, rename, config, dep-bump, doc), or testing is explicitly deferred to child tickets, PASS as not-applicable. RAISED BAR (round-4/5 over-fire fix): thin-but-present coverage is PASS, not a finding; only flag when (a) a NEW user-facing flow has happy-path-only tests with no failure/timeout/invalid/empty path; (b) a changed/deleted behavior gets no modify/remove-test work; or (c) the SELF-AUTHORED-ORACLE / change-detector anti-pattern is present — tests that snapshot current (possibly wrong) output, tautological tests, or source-greps masquerading as behavioral tests (these lock in the bug; always MAJOR). Boundary scenarios (oversized/malformed/non-Latin/back-button) and observable (not 'works correctly') outcomes are rewarded but their absence on an internal change is NOT a finding. ANTI-FP: structural greps and command-output assertions ARE a legitimate cross-language test pattern; valid TDD exemptions exist (no conditional logic, pure scaffolding, cited existing test). SEVERITY: missing failure-path on a new user-facing flow = MAJOR; change-detector/tautology = MAJOR; everything else PASS. This criterion does not run on epics (they defer tests to children) or on test-authoring tasks (the task IS the test).
