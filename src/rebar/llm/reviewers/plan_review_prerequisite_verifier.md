---
schema_version: 1
title: Plan-review prerequisite verifier
description: Verifies focused findings against the exact subject/prerequisite pair.
outputs: plan_review_verification
execution_mode: single_turn
category: plan-review-pass
---
Independently verify each listed prerequisite-consistency finding against the whole subject plan
and its exact authoritative prerequisite block. Preserve each supplied original index. Set
`binary.prerequisite_attribution_valid=yes` only when that exact pair supports the finding; set
it to `no` when the attribution is wrong or unprovable, and never use `na` for a focused finding.
Fill the remaining plan-review verification fields normally.

# Subject plan (verbatim, whole)
{{plan}}
