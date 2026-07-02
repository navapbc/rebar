---
schema_version: 1
title: Plan-review Pass-2 completion sub-call
description: 'A SEPARATE Pass-2 sub-call (epic 66ac / child 94fd) for completion-aware
  container plan-review. It receives ONLY the plan + a manifest of the container''s
  already-DELIVERED children (each with its own Acceptance Criteria) and, per finding,
  answers three atomic classification questions (attribution / containment / layer).
  It DROPS nothing itself — the later Pass-3 completion floor consumes the classification
  to drop findings that merely re-litigate delivered child work. Independent by construction:
  it is never fed the findings'' own decisions.'
outputs: plan_review_completion
execution_mode: single_turn
category: plan-review-pass
---
You are running a COMPLETION sub-call in PASS 2 of a plan review of a CONTAINER ticket (an epic
or story that is mid-flight). You are given the container's plan, a MANIFEST of its already-
DELIVERED children (each with its own Acceptance Criteria), and the current review's FINDINGS. For
EACH finding, by its 0-based index, answer these THREE atomic questions and nothing else:

- attribution — which already-DELIVERED child (from the manifest) is this finding about? Answer
  that child's ticket-id, or `none` if the finding is not about any delivered child. If the finding
  is marked PRE-ATTRIBUTED (a structural container finding), that child id is already fixed — do NOT
  re-derive it; answer only containment + layer for that finding.
- containment — is the finding's concern ENTIRELY within already-delivered child work, or does it
  reach beyond it? Answer exactly one of: `limited-to-closed` (wholly inside delivered children),
  `spans-open-or-system` (also touches open children, cross-cutting, or system-level work), or
  `n-a` (the containment question does not apply).
- layer — is the finding about the PLAN'S wording/structure/decomposition, or about the actual
  behaviour a delivered child already implements? Answer exactly one of: `plan-semantics` (about the
  plan text/decomposition), `delivered-functionality` (about behaviour a delivered child ships), or
  `n-a` (neither applies).

These are FACTUAL classification questions — do NOT judge whether the finding is valid, severe, or
whether it should be dropped; that is decided deterministically elsewhere. Be atomic: answer each
question on its own merits. When you are UNSURE, answer the fail-safe value: attribution `none`,
containment `spans-open-or-system`, layer `delivered-functionality` (these keep the finding).

<!--volatile-->
# Plan under review (verbatim, whole)
{{plan}}
