---
schema_version: 1
title: Measurability & in-session completability
description: Plan-review ac-text-quality criterion F1 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: ac-text-quality
---
Examine each acceptance/success criterion for measurability and whether an agent can complete it within ONE working session. Apply these binary checks: (a) the criterion states a specific OBSERVABLE outcome (what changes for the user/system), not effort ('implement the service') or a subjective term ('improved/better/sufficient'); (b) it is evaluable IN-SESSION via repo artifacts, the closing PR's CI, or a deterministic command against a reachable target — NOT post-sprint-only (multi-day telemetry, adoption %, survey feedback score ≤2); (c) it is a durable end-state, not a one-time transition (litmus: could it be false before this work and true only because of it?); (d) the unit is right-sized (a coherent single-outcome deliverable, not an epic-of-epics, not a one-line triviality). SEVERITY: outcome-vague or effort-framed criteria are MAJOR; post-sprint-only validation is MAJOR; thin-but-present is MINOR. ANTI-FP: evaluate the spec AS WRITTEN, not the current codebase; observability tooling itself is valid in-session work; 'post-deployment' is fine if the check is deterministic. PASS if all criteria are measurable and in-session completable.
