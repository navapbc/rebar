---
schema_version: 1
title: Acceptance-criterion process-gate redundancy
description: Plan-review criterion `ac-process-gate` (1-TURN, ac-text-quality, advisory).
  Flags an acceptance criterion whose ENTIRE completion predicate is a generic
  development-process / tooling gate CI or rebar already enforces mechanically for every
  ticket (children-closed, tests/CI/lint pass, plan-review passes, merged, commit-trailer),
  so it adds no ticket-specific signal the completion verifier can act on. Accepts an AC that
  names THIS ticket's specific deliverable, even when tests / CI / plan-review are its subject.
  Routing in criteria_routing.json. Ships advisory; promotion is a future dogfood-gated change.
  See docs/plan-review-gate.md.
execution_mode: single_turn
category: plan-review-criterion
dimension: ac-text-quality
---
GATE — apply only when the plan has at least one ACCEPTANCE-CRITERION checkbox. If it has none,
this is not-applicable → PASS.

THE DEFECT — a process-gate acceptance criterion. An AC is meant to describe what THIS ticket
delivers, so the completion verifier can check it. But some ACs merely restate a GENERIC
development-process or tooling gate that CI or rebar ALREADY ENFORCES MECHANICALLY for every
ticket, regardless of what the ticket is about. Such an AC would read IDENTICALLY, and be
satisfied identically, on an ARBITRARY unrelated ticket. It carries no ticket-specific signal:
the completion verifier cannot evaluate it as delivered work, it wastes verifier effort, and it
can mislead the verifier into gating close on a tautology already guaranteed elsewhere. The
productive review move is to ask the author to REMOVE the AC or REWORD it to the specific
deliverable it implies.

Visit EVERY acceptance-criterion checkbox independently. For each, decide: is its completion
predicate a generic mechanical gate, or this ticket's specific deliverable?

REJECT-SET — FIRE a finding on an AC whose ENTIRE content is one of these ticket-agnostic gates
(non-exhaustive; judge by meaning, not wording):
- "all child tickets / subtasks are closed / complete" — rebar's open-children guard enforces
  this structurally at close; a parent cannot close otherwise.
- "all tests pass" / "the test suite is green" / "the build passes" / "lint / typecheck / format
  pass" / "CI is green" / "Verified +1" — CI's Verified gate enforces this on every change.
- "plan review passes" / "the plan-review gate is green" / "LLM-Review +1" — the rebar / Gerrit
  plan-review gate enforces this.
- "the completion verifier passes" / "the ticket closes cleanly" — the rebar close gate enforces
  this; an AC asserting the gate that reads it is circular.
- "the change is merged / landed on main" — the landing gate enforces this.
- "every commit carries a rebar-ticket trailer / a DCO sign-off / a Change-Id" — CI's
  commit-trailer check enforces this.

ACCEPT-SET — do NOT fire (PASS) on an AC that names or turns on THIS ticket's SPECIFIC
deliverable — the artifact, behavior, contract, test, doc, config, or criterion the ticket
produces — EVEN when tests, CI, or plan-review are its SUBJECT:
- "E2E tests are written covering <this ticket's feature X>" — the tests are the deliverable
  (the meaningful predicate is that they EXIST and cover X, which the completion verifier can
  check), not a generic "the suite passes".
- "the plan-review criteria are updated to reflect <the new rubric Y>" — the rubric change IS
  the work this ticket delivers.
- "a new CI job <Z> is added that runs <W>" / "the <named> config declares <specific value>" —
  the CI job or config is the deliverable, not a generic "CI is green".
- any AC naming a specific new/removed symbol, file, behavior, or assertion unique to this ticket.

THE LITMUS (apply it to every AC): would this exact AC read, and be satisfied, IDENTICALLY on an
ARBITRARY unrelated ticket? If YES → it is a generic mechanical process gate → FIRE. If it
names or depends on this ticket's specific deliverable → PASS. The distinction is generic-gate
(mechanically enforced elsewhere) vs specific-deliverable (this ticket's work), NOT the mere
presence of the words "test", "CI", or "review".

DISTINCT FROM neighbouring criteria (do NOT double-report their concerns here):
- `evidence-kind` classifies WHERE an AC's completion proof lives (codebase-verifiable vs the
  exact operator-attested tag) and would even ACCEPT "all tests pass" when tagged. This
  criterion instead asks whether the AC is a REDUNDANT MECHANICAL GATE at all, independent of
  where its proof would live.
- `E1` owns criterion↔description coverage / duplicates; `E2` owns ambiguity; `ac-satisfiability`
  owns joint satisfiability. A process-gate AC can be unambiguous, covered, and satisfiable and
  still be a redundant mechanical gate — that residue is this criterion's miss.

This is a SINGLE-TURN plan-text judgment — reason over the acceptance-criteria text itself; you
are not grounding against the codebase here.

SEVERITY: a purely mechanical process-gate AC is MINOR — it does not send the implementer in
two directions, it just pollutes the accepted AC set. Coach: "remove this AC or reword it to the
specific deliverable it implies."

ANTI-FP — the safe direction is SILENCE. An AC that carries ANY ticket-specific deliverable
content PASSES, even if it also mentions a gate. When you are unsure whether a gate is generic
(enforced for every ticket) or specific to this ticket's work, ABSTAIN (do not fire). Never fire
merely because an AC contains the words "test", "pass", "CI", "review", or "merge".

CHECKLIST SUB-ANSWERS (criterion-local):
- has_acceptance_criteria {yes|no|insufficient} — the GATE: does the plan have at least one
  acceptance-criterion checkbox? `no` → not-applicable → PASS.
- criteria_are_deliverable_not_process_gate {yes|no|insufficient} — only meaningful when gated
  in: is every AC a ticket-specific deliverable rather than a generic mechanical gate? An AC
  whose entire predicate is a ticket-agnostic CI/rebar-enforced gate (fails the litmus) is `no`
  (the finding — cite the AC); an all-deliverable set is `yes` (PASS); an ambiguous gate is
  `insufficient` (abstain, do not assert).

ADVISORY: this criterion errs toward surfacing and coaches ("remove the process-gate AC or
reword it to the specific deliverable it implies"); it does NOT block a plan. Promotion to a
blocking posture is a future dogfood-gated `criteria_routing.json` change per the
advisory→blocking promotion gate in docs/plan-review-gate.md (the standing recorder
`criterion_effectiveness.py` auto-monitors this criterion with zero per-criterion wiring).
