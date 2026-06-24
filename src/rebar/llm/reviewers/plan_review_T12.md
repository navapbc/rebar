---
schema_version: 1
title: Rollout / rollback / reversibility [overlay]
description: Plan-review overlay-rollout criterion T12 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-rollout
---
OVERLAY — apply only when the plan changes the runtime behavior of a deployed or long-running system; else PASS not-applicable (e.g. a library/CLI with no deploy surface). Binary checks: (a) STAGED ROLLOUT: a behavior change reaches production via a flag / canary / staged rollout, not a single 100%-traffic flip. (b) ROLLBACK: there is an explicit, cheap, tested way to undo the change quickly without data cleanup. (c) DEPLOY ORDERING: if producers/consumers or coordinated services change, the deploy order (and coexistence of old+new during rollout) is specified. SEVERITY: a one-shot behavior change to all traffic with no flag and no rollback path = MAJOR. ANTI-FP: not-applicable for non-deployed code; an internal-only change with trivial revert is fine.
