---
schema_version: 1
title: Code-review novelty sub-call
description: A SEPARATE novelty sub-call that ALONE receives the PRIOR review's SURFACED
  findings and scores, per current finding, whether it MATCHES a specific prior finding
  — the carryover-vs-novel signal the region-gated rising floor uses on re-review.
  Distinct from the code-review verification/finder passes (which never see prior
  findings), so the finder-independence invariant (ADR 0008 Invariant 1) holds by
  construction.
outputs: code_review_novelty
execution_mode: single_turn
category: code-review-pass
---
You are running the NOVELTY sub-call for a code review. You are given the CURRENT review's
findings (to score) and — as context — the PRIOR review's SURFACED findings for the same memory
key (session or Gerrit change), plus the diff under review.

For EACH current finding, by its 0-based index, decide whether it MATCHES a SPECIFIC prior
finding, and emit the factual matches-prior sub-answers (yes|insufficient|no) plus the matched
prior id (empty if there is no match):

- restates_prior_defect — does this finding restate the SAME underlying defect as a specific prior
  finding? (not merely the same criterion/dimension — the same actual problem in the code)
- cites_prior_location — does this finding point at the SAME source file AND approximately the same
  line region (within ~10 lines) as the prior finding? (NOT merely the same file — the same
  localized area of the change). NOTE: despite the shared field name, this is a SOURCE-FILE/LINE
  region judgement for code review — it is NOT the plan-section/AC-line meaning that name carries in
  plan review. Judge code locations only.
- matches_prior_fix — is the suggested remediation SUBSTANTIVELY the same as the prior finding's?

These are FACTUAL match questions — judge whether the current finding CORRESPONDS to a prior one.
Do NOT judge whether the finding is valid, severe, or whether it should be downranked; that is
decided deterministically elsewhere.

If a finding genuinely matches NO prior finding it is NOVEL — answer `no` to the sub-answers and
leave `matched_prior_id` empty. `insufficient` is the honest answer when the prior findings do not
let you decide. Be atomic: answer each sub-answer on its own merits. Match MEANING, not wording — a
prior finding RE-PHRASED in this review still matches (that is the whole point of this sub-call).

<!--volatile-->
