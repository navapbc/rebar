---
schema_version: 1
title: Plan-review intent-source-fidelity (ISF) finder
description: The ISF criterion's finder — compares the plan against the EXTERNAL intent
  in the ticket's linked session log to catch silently dropped/narrowed/contradicted
  requirements. Fed the session log (single-turn, not agentic); fires only when a
  session log is linked.
outputs: plan_review_findings
execution_mode: single_turn
category: plan-review-pass
---
You are running the INTENT-SOURCE-FIDELITY (ISF) check. Compare the plan under review against
the EXTERNAL intent expressed in the ticket's LINKED SESSION LOG (the design/brainstorm of
record), to catch requirements the plan SILENTLY DROPPED, narrowed/out-scoped WITHOUT a
stated rationale, or CONTRADICTED relative to what the user expressed. (1) extract the
discrete expressed requirements/decisions/constraints from the log; (2) check the plan vs
each. ANTI-FP: a requirement DELIBERATELY descoped WITH a stated rationale is NOT a finding;
fire ONLY on a silent drop/narrowing/contradiction. Emit findings as
{finding, criteria:['ISF'], evidence[], scenarios[], impact} — no severity/confidence.

# Plan under review (verbatim, whole)
{{plan}}
