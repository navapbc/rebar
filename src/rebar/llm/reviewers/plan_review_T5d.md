---
schema_version: 1
title: Accessibility [overlay]
description: Plan-review overlay-a11y criterion T5d (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-a11y
---
OVERLAY — apply only if the plan introduces new user-facing UI; else PASS not-applicable. Binary checks: (a) wcag_compliance — does the scope address WCAG 2.1 AA with observable a11y done-definitions (keyboard, screen-reader, contrast)? (b) inclusive_ux — reduced motion, keyboard-only, screen-reader, touch-target sizing, not color-alone/mouse-only. SEVERITY: a new interactive surface with no keyboard nav = MAJOR — cite the WCAG criterion. ANTI-FP: not-applicable for backend/infra/data work.
