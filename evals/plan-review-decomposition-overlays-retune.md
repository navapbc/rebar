# Plan-Review Gate — Decomposition Review, Overlay Triggering, EXP Criterion & Proportionate-Scrutiny Re-tune (Round 4)

Decomposed epic `5fd2-a7c2-0aec-48fa` into 9 child stories (with deps) and ran the full review suite on the epic
and each child; in parallel ran the suite on a varied DSO sample, validated overlay triggering (deterministic vs
LLM), added a generic empirical-validation criterion (EXP), and diagnosed why our process is leaner than DSO's
fidelity review. ~110 LLM runs. The experiments suggested a **meaningful re-tune** — proportionate scrutiny by
ticket **level and type** — which was implemented and re-run to confirm it reduces noise without losing signal.

> **Correction (post-round-4):** the criterion called **EXP** below is the design's existing **T2 empirical probe**, not a new criterion — it was reinvented under a wrong name after being dropped from the round-1 subset. See `criteria-registry-reconciliation.md`. References to EXP = T2.

## Stream A — review the epic + 9 children

The epic is now clean (4 minor advisory findings; **G5 decomposition resolved** once it had children). But the
**children (stories) drew 7–10 mostly-MAJOR findings each** — the gate applied full *leaf-implementation*
scrutiny (E6 proving-commands, leaf overlays T5a/b/c, "every value defined") to mid-level workstream stories that
legitimately defer that detail to their constituent tasks. This is the round's central tuning signal.

## Stream B — varied DSO sample (12 tickets: epics incl. one without `brainstorm:complete`, stories, tasks, bugs)

- **EXP criterion (new) behaves correctly:** suppressed (PASS) on the `bc47` placeholder *pilot* epic (it already
  is an experiment) and on every mechanical task/bug; fired only on `1414` (an asserted `batch=20` threshold with
  no derivation). The suppression-when-experimentation-present logic holds.
- **Bugs drew 6–7 planning-findings each** (F1/E1/E6 firing because bugs have no acceptance-criteria shape) —
  empirically confirming the design's decision to **exempt bugs** (route to the F6 follow-on), not a criteria bug.
- **E5/E6/F4/overlays over-fire on mechanical & test tasks** (e.g., E5 "thin testing" on a RED-test task that *is*
  the test; F4 "no user" on an internal leaf task) — a **type**-mismatch, the second tuning signal.

## Stream C — overlay triggering: deterministic-where-low-FP, LLM-router otherwise (validated)

For each ticket, a deterministic keyword/signal detector and an LLM relevance router were compared:

| overlay | det vs LLM agreement | verdict |
|---|---|---|
| **T1** prior-art | 13/13, 0 det-only | **deterministic** (the 6 bright-lines are low-FP) |
| **T5a** perf | 12/13, 1 det-only | **deterministic** |
| **T5d** a11y | 13/13 (UI-keyword) | **deterministic** (low-FP UI-keyword count) |
| **T5e** maint / **T7** docs | agree but det under-fires | deterministic prior + LLM confirm |
| **T8** LLM-antipatterns | det fires 9, LLM 3 (6 det-only) | **LLM-route** — "agent/prompt/LLM" matches almost everything |
| **T6** UX | det 0, LLM 7 | **LLM-route** — no reliable keyword |
| **T5b** reliability / **T5c** security / **T9** | mixed | **LLM-route** |

→ Confirms the approach: fire **T1/T5a/T5d deterministically**; **LLM-route T8/T6/T5b/T5c/T9** (T8 is the clearest
— a keyword trigger is high-FP). This is now encoded per-overlay in the registry `trigger` field.

## Stream D — why our process is leaner than DSO's fidelity review (signal-loss check)

Compared our findings to DSO's recorded PIL (Planning-Intelligence-Log) review findings on 3 DSO epics:

| DSO epic | DSO recorded | our single-turn | our T8 structural-gap probe |
|---|---|---|---|
| b575 (arbiter) | 12 | 7 | **FAIL — found 5 structural gaps** (e.g. "`cross_reviewer_agreement` enum vocabulary *differs* between plan and the live agent file") |
| 4100 (verifier) | 5 | 7 | n/a |
| e7f3 (calibration) | 8 | 8 | PASS (correctly — DSO's volume here was forced-scoring revisions, not structural gaps) |

**We are NOT losing signal.** DSO's higher volume on b575 is its bot-psychologist *structural-gap* findings —
which **our T8 agentic probe recovers** (5 of DSO's 6, including the same enum-vocabulary-mismatch class). DSO's
extra volume on e7f3 is low-actionability forced per-dimension scoring (3→5 revisions) that our anti-FP, coaching
posture correctly drops. The **only** real signal-loss risk was **under-triggering T8** — which the overlay router
(Stream C) fixes by firing T8 on LLM/agent-design plans. Net: our leanness = less noise, same signal.

## The re-tune: proportionate scrutiny by LEVEL and TYPE (implemented + re-run)

The criteria descriptor gains an `applies_at` (levels) field; the orchestrator filters by the ticket's level and
type before chunking:

| | epic (has_children) | story (mid-level) | task / leaf | bug |
|---|---|---|---|---|
| **judgment** | F4, E1, E3, G5, EXP, container G3/G4 | F1, F4, E1, E2, E3, E5(strategy), G5, T5e, EXP | **full set** F1,E2,E5,E6,G1G2,E4,A1 | **exempt** → bug-review follow-on (F6) |
| **suppressed** | leaf-grain E6/E2-detail/G1G2/E4/A1; leaf overlays T5a/b/c | leaf-grain E6/G1G2/E4; leaf overlays | — | all planning criteria |

Plus **type rules**: E5 suppressed on RED/GREEN test tasks (the task *is* the test); F4 N/A on internal/mechanical
leaf tasks (implied technical consumer); E6 satisfied by a ticket's own `Verify:` commands.

**Re-run validation (story altitude, leaf-grain criteria suppressed):** children dropped from **7–10 → 3–8
findings**, concentrated on the *real* ones (E2 genuine ambiguities, E5 testing-strategy, G5 decompose-into-tasks,
EXP unvalidated claims, T5e architecture); leaf-noise (E6 proving-commands, deep perf/reliability) gone. E5
correctly suppressed on RED test-tasks. The over-firing was altitude/type-mismatch, not bad criteria — and
level+type routing fixes it.

## Changes made

1. **EXP criterion** added to the registry (`criteria_v3.json`) — generic empirical-validation check, **suppressed
   when the plan already includes experimentation** (a spike, fixture/RED test, pilot with metrics, or measurement
   step). The review process now coaches complex/novel plans to validate empirically — *generically*, and the
   guidance does **not** fire when experimentation is already present.
2. **Proportionate-scrutiny `applies_at` routing** by level + type, in the orchestrator (epic/story/task/bug).
3. **Overlay trigger split** per Stream C: T1/T5a/T5d deterministic; T8/T6/T5b/T5c/T9 LLM-routed; recorded in the
   registry `trigger` field. **T8 is in the active set** for LLM/agent-design plans (recovers structural-gap signal).
4. Epic + the `overlays` and `layer2` child stories updated to reflect 2–3.

## Tuning verdict

The criteria themselves are well-calibrated (low-FP, addressable, convergent — round 3). The round-4 issue was
**routing**: applying one strictness/criteria-set to all levels and types. With level+type `applies_at` routing
and the deterministic/LLM overlay split, the gate stays lean *and* keeps signal (T8 recovers what DSO's
bot-psychologist finds). No criteria-content redesign needed; the changes are routing + one new criterion (EXP).

Data: `plan-review-gate/runs/r4_A.jsonl` (epic+children), `plan-review-gate/runs/r4_B.jsonl` (DSO sample), `plan-review-gate/runs/r4_C.jsonl` (overlay triggering),
`plan-review-gate/runs/r4_D.jsonl` + `plan-review-gate/runs/r4_T8.json` (PIL comparison), `plan-review-gate/runs/retune_out.json` (level-aware re-run);
`plan-review-gate/criteria/criteria_v3.json` (incl EXP); harnesses `plan-review-gate/harnesses/round4.py`, `plan-review-gate/harnesses/retune.py`; analysis `plan-review-gate/harnesses/analyze_r4.py`.
