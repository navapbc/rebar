---
schema_version: 1
title: Reliability (overlay)
description: Plan-review overlay-reliability criterion T5b (1-TURN). The rubric the
  Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-reliability
---
OVERLAY — apply only if the plan adds failure points (external integration, file I/O, LLM calls), write operations, or stateful transitions; otherwise PASS as not-applicable. Binary checks: (a) error_handling — retry/backoff/circuit-breaker/graceful-degradation is present and error states are surfaced, not swallowed; (b) failover — recovery happens without data loss or corruption, writes are idempotent, partial state is safe/durable. SEVERITY: an external call with NO error handling is MAJOR; missing idempotency on a write is MAJOR. Blast-radius is a tiebreaker that only LOWERS severity, never raises it. ANTI-FP: failover is not-applicable (PASS) if there are no writes/state/external deps. PASS if the plan fails safely. ALSO check OBSERVABILITY (are new failure points instrumented with a metric/log/trace/alert so operators can see and debug them?) and DEPENDENCY-FAILURE blast radius (when a hard external dep is down/slow: timeout, circuit-breaker, fallback/degraded-mode, or does the feature — or an unrelated one — go down?).
