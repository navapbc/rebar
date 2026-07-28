---
schema_version: 1
title: No-file-impact declaration consistency
description: Advisory plan-review criterion for explicit no-file-impact declarations.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
Evaluate an explicit declaration that this ticket has **no repository file impact**. The
authoritative persisted declaration context is supplied immediately before this rubric.

Answer all three criterion-local sub-answers before deciding whether to surface a finding:

1. `assertion_plausible: yes|no|insufficient`: Is the `none` declaration consistent with every
   required deliverable?
2. `reason_grounded: yes|no|insufficient`: Does the supplied `declared_reason` concretely explain
   why no repository artifact is needed?
3. `repo_file_change_named: yes|no`: Does the plan name a repository edit — source, tests,
   configuration, CI/automation, a generated tracked artifact, or documentation?

Set `assertion_plausible: no` when `repo_file_change_named: yes` or when a required deliverable
otherwise entails a repository edit. Surface an advisory contradiction finding that names the
deliverable and coaches the author to declare its paths. Documentation edits ARE repository
edits.

Set `assertion_plausible: yes` only when `reason_grounded: yes`,
`repo_file_change_named: no`, and every required deliverable is outside the repository. Return
PASS with no finding.

Set `assertion_plausible: insufficient` when `reason_grounded` is `no` or `insufficient`,
`repo_file_change_named: no`, and the plan is too vague to establish either outcome. Return
non-blocking coaching asking the author to name each deliverable and give an auditable
external-only rationale. Never invent an edit merely because the reason is weak.

ANTI-FP: External operator actions, changes in third-party systems, and other work whose
deliverable truly occurs outside this repository pass this criterion. Do not require code,
tests, or docs for such work. This criterion is advisory even when the declaration contradicts
the plan; report a precise, plan-grounded correction rather than blocking the ticket.
