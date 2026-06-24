---
schema_version: 1
title: Performance (overlay)
description: Plan-review overlay-perf criterion T5a (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-perf
---
OVERLAY — apply only if the plan introduces new I/O, data access, LLM/compute calls, batch ops, or shared resources; otherwise PASS as not-applicable. Binary checks: (a) latency — hot-path operations have time-bounded done-definitions and no synchronous blocking on a hot path; (b) resource_efficiency — no N+1 / redundant API or LLM calls / unbounded memory growth; (c) scalability — input-size limits, concurrency/rate-limit/pool handling, and load expectations are stated. SEVERITY: a user-facing operation with no latency target is MAJOR; an O(n) LLM-calls-per-item pattern is MAJOR — state the impact in Big-O terms. ANTI-FP: score normally only where the plan actually adds a performance-relevant path. PASS if performance characteristics are sound for the scope. ALSO assess COST/economics (not just latency): per-call $ (e.g. an LLM/embedding call per item), egress, always-on vs serverless, unbounded fan-out — a design can be fast and ruinously expensive.
