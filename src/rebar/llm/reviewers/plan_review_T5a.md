---
schema_version: 1
title: Performance (overlay)
description: Plan-review overlay-perf criterion T5a (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-perf
---
OVERLAY — apply only if the plan introduces new I/O, data access, LLM/compute calls, batch ops, or shared resources; otherwise PASS as not-applicable. Binary checks: (a) latency — hot-path operations have time-bounded done-definitions and no synchronous blocking on a hot path; (b) resource_efficiency — no N+1 / redundant API or LLM calls / unbounded memory growth; (c) scalability — input-size limits, concurrency/rate-limit/pool handling, and load expectations are stated. SEVERITY: a user-facing operation with no latency target is MAJOR; an O(n) LLM-calls-per-item pattern is MAJOR — state the impact in Big-O terms. ANTI-FP: score normally only where the plan actually adds a performance-relevant path. PASS if performance characteristics are sound for the scope. ALSO assess COST/economics (not just latency): per-call $ (e.g. an LLM/embedding call per item), egress, always-on vs serverless, unbounded fan-out — a design can be fast and ruinously expensive. SCALE-INFERENCE ANCHOR (G-9 — small-scale default): assume small scale unless the plan supplies evidence (a scale estimate, a profiling result, or an explicit AC). Never assume higher scale than the evidence supports. Prohibited reasoning: do not interpolate volume from the subject matter — "handles millions" or "it's a government portal" are NOT usable estimates; scale sensitivity is orthogonal to volume. This bar is two-directional: fire a perf finding only on evidenced scale, and do NOT demand scale handling the plan never claims.
