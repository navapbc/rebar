---
schema_version: 1
title: Plan-review validation contradiction cross-check
description: A validation-assessment sub-call (bug 5e40) that cross-checks the findings
  within ONE verdict for MUTUAL contradiction and names the false/contradicted member
  to drop — the intra-verdict consistency check that kills a false BLOCK refuted by
  a true advisory in the same verdict.
outputs: plan_review_contradiction
execution_mode: single_turn
category: plan-review-pass
---
You are running a VALIDATION cross-check over the findings of a SINGLE plan-review verdict. You
are given the current review's findings, each with a 0-based index. Your ONLY job is to find
pairs of findings that MUTUALLY CONTRADICT one another — where BOTH cannot be true of the same
plan at the same time — and, for each such pair, say WHICH member is the FALSE/contradicted one
(the one to drop).

The canonical case: one finding asserts a thing is ABSENT ("no one is tasked with X", "the plan
never captures Y") while another finding in the SAME verdict asserts that exact thing is PRESENT
("the parent explicitly assigns X to S1", "Y is captured before cutover"). Both cannot hold — the
"absent" finding is the false one and must be dropped.

For EACH contradictory pair emit `{a, b, contradiction: true, drop, rationale}`:

- `a`, `b` — the two finding indices.
- `contradiction` — true ONLY when the two are genuinely mutually exclusive. Two findings about
  the same area that merely OVERLAP, or that raise DIFFERENT gaps, are NOT a contradiction — leave
  them alone.
- `drop` — the index (`a` or `b`) of the member that is FALSE / refuted by the other. If the plan
  text itself, or the other finding, shows one to be factually wrong, that wrong one is `drop`. Use
  `-1` only when they truly contradict but you cannot tell which is wrong.
- `rationale` — one sentence.

Emit NOTHING for findings that do not contradict any other. Do NOT judge severity, novelty, or
whether a single finding is valid on its own — only pairwise contradiction. Judge by MEANING, not
wording. When in doubt whether two findings contradict, DO NOT emit the pair (the safe direction
is to keep both).

<!--volatile-->
# Plan under review (verbatim, whole)
{{plan}}
