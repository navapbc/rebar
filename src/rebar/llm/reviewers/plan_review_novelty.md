---
schema_version: 1
title: Plan-review Pass-2 novelty sub-call
description: A SEPARATE Pass-2 sub-call (child 150b) that ALONE receives the PRIOR-review
  findings and scores, per current finding, whether it MATCHES a specific prior finding
  — the carryover-vs-novel signal the Pass-3 rising floor uses on a remediation re-review.
  Distinct from the verification sub-call (which never sees the prior findings), so
  the independence invariant holds by construction.
outputs: plan_review_novelty
execution_mode: single_turn
category: plan-review-pass
---
You are running a NOVELTY sub-call in PASS 2 of a plan review. You are given the CURRENT
review's findings and — as context — the PRIOR review's findings. For EACH current finding, by
its 0-based index, decide whether it MATCHES a SPECIFIC prior finding, and emit the factual
matches-prior sub-answers (yes|insufficient|no) plus the matched prior id (empty if no match):

- restates_prior_defect — does this finding restate the SAME underlying defect as a specific
  prior finding? (not merely the same criterion — the same actual problem)
- cites_prior_location — does it point at the SAME plan location / section / AC line as that
  prior finding?
- matches_prior_fix — is its suggested remediation SUBSTANTIVELY the same as that prior
  finding's?

These are FACTUAL match questions — judge whether the current finding corresponds to a prior
one. Do NOT judge whether the finding is valid, severe, or whether it should be downranked;
that is decided deterministically elsewhere. A finding that genuinely matches NO prior finding
is novel — answer `no` to the sub-answers and leave `matched_prior_id` empty. 'insufficient' is
an honest answer when the prior findings do not decide it. Be atomic: answer each sub-answer on
its own merits. Match by MEANING, not wording — a prior finding RE-PHRASED in this review is
still a match (that is the whole point of this sub-call).

<!--volatile-->
# Plan under review (verbatim, whole)
{{plan}}
