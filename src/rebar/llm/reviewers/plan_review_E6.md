---
schema_version: 1
title: Verification/termination + end-state reachability
description: Plan-review ac-text-quality criterion E6 (2-STEP). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: ac-text-quality
---
Check that the work has a clear way to be verified done and that the steps actually reach the stated end-state (a two-step check: identify the proving command for each claim, then confirm the union of steps proves every criterion). Binary checks: (a) every completion-relevant claim has a concrete proving command/check that would produce evidence on success (not 'should work'); (b) the claim is free of red-flag hedges ('should', 'probably', 'seems'); (c) every acceptance criterion maps to at least one described step; (d) the UNION of the steps actually reaches the stated end-state — no gap between 'what we'll do' and 'what done looks like'; (e) user-facing flows have an end-to-end check or a documented rationale for its absence. SEVERITY: a criterion no step reaches, or a claimed-but-unmeasured outcome, is MAJOR; a hedge without a proving command is MINOR. ANTI-FP: universal lint/format/test commands do not by themselves prove a specific criterion. PASS if done-ness is verifiable and the end-state is reachable.
