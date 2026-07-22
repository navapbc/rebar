---
schema_version: 1
title: Plan-review prerequisite finder
description: Independently evaluates the subject plan against each direct prerequisite.
outputs: plan_review_prerequisite_coverage
execution_mode: single_turn
category: plan-review-pass
---
Review only consistency between the subject plan and each delimited direct prerequisite supplied
in the instructions. Judge every pair independently; never derive a finding from comparisons
between prerequisites. Return exactly one record per authoritative prerequisite id. A clean pair
is `consistent`; an incompatibility is `finding`. Never emit `indeterminate`; runtime and schema
failures are classified deterministically by the caller. Findings use
only criterion `prerequisite-consistency` and repeat the exact authoritative prerequisite id.

# SUBJECT_PLAN BEGIN (verbatim, whole)
{{plan}}
# SUBJECT_PLAN END
