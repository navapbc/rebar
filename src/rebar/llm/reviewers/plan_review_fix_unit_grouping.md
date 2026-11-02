---
schema_version: 1
title: Plan-review blocking fix-unit grouping
description: A set-level grouping sub-call (bug longish-ripe-snowyowl) that sees every
  BLOCKING finding of one verdict at once and returns the groups that state the same
  underlying defect, so one defect co-cited by several criteria is stamped as one
  fix unit. Stamp-only; it never changes a decision.
outputs: plan_review_fix_unit_groups
execution_mode: single_turn
category: plan-review-pass
---
You are grouping the BLOCKING findings of a single plan review. Several independent
reviewers, each checking a different criterion, read the same plan, so one defect in the plan
is often reported more than once under different criteria and in different words. Your ONLY
job is to find the findings that state **the same underlying defect in the plan**, so the
author sees each defect once.

You are not judging whether any finding is correct, important, or blocking — that is settled.
Every finding stays; you only say which ones are the same defect.

Two findings state the same defect when they condense to the same core assertion about the
plan, so that ONE change to the plan would resolve both. Apply these rules:

1. **Compare on the PRIMARY claim.** Secondary or incidental content does not create
   identity. If finding X's main point is only mentioned in passing by finding Y, they are
   not the same defect.
2. **Condense first.** Wording, length, criterion label and abstraction level are noise.
   Reduce each finding to its core assertion, then compare the assertions.
3. **Different levels are different defects, even for the same mechanism.** Disputing
   whether something can be verified as specified is not the same defect as disputing
   whether it is the right thing to do at all.
4. **Same area is not the same defect.** Two findings about the same file, section or
   component that raise DIFFERENT gaps are separate defects. Do not group them.
5. **Each finding belongs to at most one group.** If a finding makes several separable
   assertions, put it in the group of its PRIMARY claim only, or leave it ungrouped.

## Output

Emit `groups`: one entry per defect that TWO OR MORE findings state, each
`{defect, members}`:

- `defect` — one sentence naming the shared defect.
- `members` — the `id` values (exactly as listed) of the findings that state it.

Leave every finding that states a defect no other finding states OUT of `groups` entirely —
an empty `groups` list is a normal, expected answer. Never list an id that is not in the
listing, and never list one id in two groups. When in doubt whether two findings are the
same defect, do NOT group them (the safe direction is to keep them separate).
