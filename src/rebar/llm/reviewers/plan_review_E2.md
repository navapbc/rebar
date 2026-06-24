---
schema_version: 1
title: Ambiguity / executable-without-clarification
description: Plan-review ac-text-quality criterion E2 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: ac-text-quality
---
Decide whether an executing agent could act on this plan WITHOUT stopping to ask a clarifying question. Run the 6-signal ambiguity scan: (1) undefined scope boundaries ('improve performance' — of what, by how much); (2) implicit acceptance criteria (types/size limits unstated); (3) conflicting signals (title says X, body Y); (4) missing persona (admin vs end-user); (5) unstated constraints (an API with no auth/rate-limit mention); (6) ambiguous priority (essential vs nice-to-have unranked). Plus flag any scope bullet that is a PLACEHOLDER not a decision: contains 'verify whether', 'check if', 'TBD', 'figure out', 'depends on investigation', or defers a real design choice to the executor ('choose an appropriate X'). SEVERITY: an ambiguity that BLOCKS planning ('cannot proceed without this') is MAJOR; a defaultable gap ('assume X unless told') is MINOR. ANTI-FP: never flag something clearly inferrable from the parent epic or an obvious convention. PASS if the plan is executable without clarification.
