---
schema_version: 1
title: Maintainability (overlay)
description: Plan-review overlay-maintainability criterion T5e (1-TURN). The rubric
  the Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-maintainability
---
OVERLAY — apply only if the plan crosses component boundaries, adds business rules/thresholds/integration points, or introduces a new pattern/contract/pipeline stage; otherwise PASS as not-applicable. Binary checks: (a) coupling_risk — new cross-component dependencies are acknowledged, justified, and mitigated (via an interface or event boundary), not silently introduced; (b) changeability — rules/thresholds expected to evolve are configurable, not hardcoded; (c) documentation — a novel architectural decision is captured in an ADR / AGENTS.md / design doc update. SEVERITY: a new pipeline stage or cross-component coupling with no doc/ADR is MAJOR on documentation; hardcoded soon-to-change thresholds are MINOR. ANTI-FP: each sub-check is not-applicable (PASS) where the plan introduces no new coupling/rules/decisions. PASS if the change keeps the system maintainable.
