---
schema_version: 1
title: Overlay de-risk — prove risky mechanisms out-of-loop
description: Plan-review overlay T15 (AGENT, LLM-routed). The rubric the Pass-1 finder
  applies when a plan relies on a slow/costly codified loop (CI pipeline, environment/
  infrastructure apply) to validate runtime-only correctness; routing in criteria_routing.json.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-derisk
---
OVERLAY — apply only when ALL of the following hold; else PASS not-applicable.
- S1 Codified loop? the mechanism is delivered/validated by pushing through an automated pipeline or an environment/infrastructure apply, not by running or unit-testing code directly.
- S2 Slow or costly pass? one pass through that loop is slow (minutes-to-hours) or otherwise costly to run.
- S3 Runtime-only correctness? the change introduces behavior whose correctness only resolves when it actually runs in a realistic environment (boots, authenticates, connects, is authorized, passes readiness) — unreachable by static/unit checks.

When applicable, report a finding for each check not satisfied:
- (a) RISK NAMED — the plan identifies the specific mechanism(s) confirmable only by completing the full loop.
- (b) FAST OUT-OF-LOOP PROOF — for each named risk, the plan commits to proving it via the fastest feedback path available for that mechanism, outside the slow loop: running the unit locally/emulated where feasible, OR a manual experiment directly against the real target (an interactive call, a one-off CLI/console action, a probe script, a throwaway resource) where local reproduction isn't possible. The experiment is concrete and returns a verdict in minutes, not a delivery cycle. An unnamed "we'll verify" / "we'll check after deploy" does not satisfy it.
- (c) PROVE-THEN-CODIFY — the cheap experiment comes before committing the mechanism into the slow loop, so the loop validates an already-proven mechanism.
- (d) SCOPED CLEANUP — throwaway experiments are cleaned up afterward, with teardown that removes ONLY the artifacts the experiment itself created (the records a probe inserted, the scratch resource it spun up) and touches nothing pre-existing, shared, or persistent; a cleanup step that could delete, reset, or truncate a resource the experiment did not create (e.g. dropping an existing table or wiping a bucket to "reset" it) fails this check.

ANTI-FP: a manual experiment against the real target is a fully valid proof — never penalize choosing it over local reproduction; many stacks cannot be reproduced locally. A mechanism already proven elsewhere in the codebase needn't be re-proven.
