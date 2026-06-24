---
schema_version: 1
title: Intent-source fidelity (plan vs linked design intent)
description: Plan-review intent-provenance criterion ISF (2-STEP). The rubric the
  Pass-1 finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: intent-provenance
---
Compare the plan against the EXTERNAL intent expressed in the ticket's LINKED SESSION LOG (the design/brainstorm of record), to catch requirements the plan SILENTLY DROPPED, descoped, or contradicted relative to what the user expressed — a defect no plan-internal check can catch (E3 compares plan-vs-its-own-title; this compares plan-vs-the-original-intent). 2-STEP: (1) extract the discrete expressed requirements/decisions/constraints from the linked session log; (2) check the plan + its ticket graph against each, flagging any dropped, narrowed/out-scoped-without-rationale, or contradicted. Runs on a FRONTIER model (large session-log context) and is FED the session log + the pre-resolved ticket graph as context — NOT agent/tool-using (deterministic if the linked log exceeds the escalated context window, evaluate against a SUMMARY of the log and RECORD that a summary was used — the finding then carries REDUCED CONFIDENCE). ANTI-FP: a requirement DELIBERATELY descoped WITH a stated rationale is not a finding; fire only on SILENT or unjustified divergence.
