---
schema_version: 1
title: Documentation [overlay]
description: Plan-review overlay-docs criterion T7 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-docs
---
OVERLAY — apply when the plan introduces something that needs documenting or invalidates existing docs; else PASS not-applicable. Checks: (a) NEW-needed — a new pattern/contract/config/CLI gets a doc/ADR? (b) INVALIDATED — does the change make existing docs/references stale (deleted/renamed artifacts still referenced)? (c) not-excessive / navigable — large docs have structure; no hot-path instruction-bloat. SEVERITY: a new architectural decision with no ADR/doc, or a change that strands stale references = MAJOR. ANTI-FP: trivial/internal changes need no doc.
