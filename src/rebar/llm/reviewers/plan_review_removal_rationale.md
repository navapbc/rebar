---
schema_version: 1
title: Removal rationale (Chesterton's Fence)
description: Plan-review criterion `removal-rationale` (AGENT, code-grounded, advisory).
  The removal-side dual of A1 — don't tear down a fence until you understand why it
  was built. Routing in criteria_routing.json. See docs/plan-review-gate.md.
execution_mode: agentic
category: plan-review-criterion
dimension: codebase-grounding
---
OVERLAY / GATE — apply only when the plan REMOVES or WEAKENS something whose purpose may be
non-obvious. Fire if ANY of these bright-line triggers holds (a disjunction — no subjective
"is this incidental?" judgment):

1. The plan removes or weakens an EXTERNALLY-OBSERVABLE behavior or contract on ANY path —
   including failure / timeout / invalid / boundary / exception semantics. "Internal" is defined by
   observable-behavior PRESERVATION, not file locality: a refactor that swallows an error, changes
   an exception type, turns retry into fail-fast, or shrinks a timeout is NOT exempt — failure-mode
   behavior is outward-facing.
2. The plan removes or weakens a check / test / validation that GUARDS such a behavior. (Tests are
   in scope; any overlap with E5's changed-behavior-tests is resolved by the Pass-4 coaching pass
   grouping the two, not by partitioning the criteria.)
3. The plan removes an artifact carrying an EXPLICIT INTENT MARKER — an explanatory comment, a
   `# do not remove`, a referenced bug/ticket, or a test named after a bug. Use your tools to
   confirm the marker exists (Grep/Read/blame); this is objective, grep-able, not a vibe.

EXEMPT (PASS / not-applicable) — rebar values legitimate simplification, so this criterion must
never nag it: dead-code removal, a pure internal simplification that preserves ALL observable
behavior including error/failure semantics, and mechanical/config/doc changes with no behavioral
delta.

PASS DEMONSTRATION (the intent is "show we understand what we're changing," NOT "poke holes"): the
plan must supply a concrete TRIGGERING SCENARIO — the input/condition under which the removed
behavior or guard mattered — GROUNDED in evidence (the explanatory comment / a pinning test /
git-blame / a linked ticket / a spec-named input class), NOT invented — PLUS evidence the reason no
longer applies (handled elsewhere / precondition now guaranteed / contract intentionally changed and
updated). Verify the cited grounding with your tools; a scenario you cannot corroborate in the code
is ungrounded. This grounded scenario is a specification-by-example of the fence (coach move 6).

CHECKLIST SUB-ANSWERS (criterion-local):
- removes_external_behavior_or_guarded_fence {yes|no|insufficient} — the GATE (the disjunction
  above). `no` → not-applicable → PASS.
- removal_scenario_grounded {yes|no|insufficient} — only meaningful when gated in: does the plan
  give a concrete scenario where the removed behavior/guard mattered, GROUNDED in a
  comment/test/blame/linked-ticket (not invented), plus evidence the reason no longer applies? A
  fabricated or ungrounded justification is `no`.

ACCEPTED LIMITATION (log in coverage, do not hide): a purely-latent guard whose removal changes
behavior only for inputs never exercised today AND which carries no intent marker will NOT fire — it
is indistinguishable from dead code without an external signal, and chasing it is the un-scalable
nag we are avoiding. Record this as a coverage note, not a silent cap.

ADVISORY: this criterion errs toward surfacing and coaches; it does not block a claim.
