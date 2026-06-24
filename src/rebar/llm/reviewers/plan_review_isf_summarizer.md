---
schema_version: 1
title: Plan-review ISF session-log summarizer
description: Compresses an oversized linked session log to fit the ISF context window
  — preserving the discrete expressed requirements/decisions/constraints. Used ONLY
  for the supporting log context (the plan is never summarized); ISF findings then
  carry reduced confidence.
execution_mode: single_turn
category: plan-review-pass
---
Summarize the following design/brainstorm session log into its discrete expressed
REQUIREMENTS, DECISIONS, and CONSTRAINTS — preserve every distinct intent verbatim enough to
check a plan against it; drop only narrative/repetition.
