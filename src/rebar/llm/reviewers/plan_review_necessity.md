---
schema_version: 1
title: Necessity / no-op probe
description: Plan-review criterion `necessity` (1-TURN, scope-intent, advisory). Flags a plan
  that does NOT demonstrate the change is needed — current behavior neither reproduced nor
  motivated (the FixedBench over-action gap). Accepts a well-motivated plan and a justified
  no-op / docs-only / test-only outcome. Routing in criteria_routing.json. Also the sole
  criterion of the light BUG REVIEW TIER. Ships advisory; promotion is a future dogfood-gated
  change. See docs/plan-review-gate.md.
execution_mode: single_turn
category: plan-review-criterion
dimension: scope-intent
---
GATE — apply only when the plan PROPOSES A CHANGE to existing behavior or code: it adds a
mechanism, alters a code path, or fixes a defect. If the plan is a pure investigation / spike /
doc-only / test-only plan, or its deliverable is an EXPLICITLY justified no-op, this is
not-applicable → PASS.

THE DEFECT — over-action without demonstrated necessity. FixedBench found that 35-65% of agent
changes were taken WITHOUT demonstrating that the change was needed: a mechanism is added, a code
path altered, a "fix" applied, but the plan never establishes that the CURRENT behavior is wrong,
insufficient, or reproduces the problem. The productive review move is to ask the planner to
DEMONSTRATE the need before building — reproduce or concretely motivate the current behavior the
change targets.

FIRE A FINDING when the plan proposes a change but does NOT demonstrate its necessity — there is:
- no REPRODUCTION of the current behavior (no steps / Expected-vs-Actual / failing case), AND
- no concrete MOTIVATION (no named defect, gap, or user/system problem the current behavior
  causes) — the plan asserts a change is wanted but never shows the status quo is wrong.

ACCEPT (PASS) when EITHER:
- the plan MOTIVATES the change — it reproduces the current behavior (repro steps, an
  Expected/Actual, a failing test), OR names a concrete defect/gap/user-problem the current
  behavior causes; OR
- the deliverable is a justified no-op / docs-only / test-only outcome the plan states as such.

DISTINCT FROM neighbouring criteria (do NOT double-report their concerns here):
- R1 `asserted-capability` greps whether a NAMED MODULE already provides (or lacks) the
  capability the plan relies on — a code-grounded surface check. `necessity` instead judges,
  from the plan text, whether the change is MOTIVATED at all. A plan can name a real module and
  still fail to motivate why the current behavior needs changing (that is this criterion's miss).
- E4 is the broad assertion/existence probe (blocking). `necessity` is the over-action /
  no-demonstrated-need probe (advisory).
- F4 asks WHO benefits; `necessity` asks whether the CURRENT behavior is shown to be wrong.

This is a SINGLE-TURN plan-text judgment — reason over the plan's own Why / Reproduction /
Motivation / context sections; you are not grounding against the codebase here.

CHECKLIST SUB-ANSWERS (criterion-local):
- proposes_a_change {yes|no|insufficient} — the GATE: does the plan propose a change to existing
  behavior/code (not a pure investigation/doc/test-only plan, nor a stated justified no-op)?
  `no` → not-applicable → PASS.
- demonstrates_necessity {yes|no|insufficient} — only meaningful when gated in: does the plan
  demonstrate the change is needed (reproduces OR concretely motivates the current behavior)?
  A change proposed with NO reproduction and NO motivation is `no` (the over-action miss — a
  finding); a well-motivated plan is `yes` (PASS); an ambiguous case is `insufficient` (coach,
  do not assert).

ADVISORY: this criterion errs toward surfacing and coaches ("demonstrate the current behavior /
motivate the need before adding the mechanism"); it does NOT block a plan. Promotion to a
blocking posture is a future dogfood-gated `criteria_routing.json` change per the
advisory→blocking promotion gate in docs/plan-review-gate.md (the standing recorder
`criterion_effectiveness.py` auto-monitors this criterion with zero per-criterion wiring).
