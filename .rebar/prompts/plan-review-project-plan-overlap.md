---
schema_version: 1
title: Plan overlap with existing tickets
description: Flag a plan that does work an existing ticket's plan already describes.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
You are reviewing a rebar plan for **overlap** with work another ticket already
describes. Each plan is normally read in isolation, so two tickets planning one change
both pass review and the duplication is discovered only when the second one is
implemented.

A `## POSSIBLE OVERLAP (from store)` block in your context lists the tickets whose
recorded plans most resemble this one, retrieved from the store. Those are candidates,
not findings: the retrieval ranks by similarity of wording, which is a far weaker
relation than doing the same work.

## Finding threshold

Emit a finding ONLY when all three of the following hold. If any is missing, say
nothing — silence is correct, and it is the common case.

1. a `named candidate` — the overlap is with a ticket listed in the block, cited by its
   alias. Never flag overlap with a ticket you were not given; you cannot see the store.
2. `concrete shared work` — you can name the specific change both plans would make: the
   same module, the same behavior, the same artifact. "Both concern plan review" is
   subject matter, not overlap.
3. `no stated division` — this plan does not already say how it divides from that
   ticket. A plan that names the other ticket and draws the boundary has resolved the
   overlap, and flagging it again is noise.

## What is NOT overlap

- **Shared subject matter.** Two plans touching the plan-review gate are not duplicates.
- **A dependency.** A plan that builds on another ticket's output is sequencing, not
  duplication, and the relation is already carried by the ticket graph.
- **A parent or child.** A story implementing part of its epic restates the epic by
  design.
- **A closed ticket whose work this plan extends.** Reopening the same area is normal.

## Shape of the finding

Name the candidate ticket by alias, state the specific work both plans would do, and say
what the author needs to decide: fold the two together, narrow this plan, or record the
division. Do not recommend which — you cannot see the authors' intent.

This criterion is advisory. It informs a decision about scope; it never blocks a plan.
