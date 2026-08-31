---
schema_version: 1
title: Failure-disposition contract
description: Plan-review criterion `project.failure-disposition-contract` (1-TURN,
  project-invariants, advisory). Flags a plan that ADDS or ALTERS
  failure/timeout/exception/retry/fallback/circuit-breaker semantics but does NOT
  state its failure-disposition contract — per affected arm, whether the surfaced
  disposition is retryable/transient vs fatal/permanent; and, when a fallback chain
  exists, which leg wins on fallback-failure. Self-gating (PASS when the plan touches
  no such semantics). Routing in `.rebar/criteria_routing.json`. Ships advisory;
  promotion is a future dogfood-gated change. See docs/plan-review-gate.md.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
GATE — apply only when the plan ADDS or ALTERS failure / timeout / exception / retry / fallback
/ circuit-breaker semantics on some path: it adds a fallback (or failover) arm, changes an
exception type or what disposition a path surfaces, changes retry / backoff / jitter / deadline
behavior, or changes how a degraded / unreachable dependency is handled. This is detected from
the plan text AND the declared `file_impact` (a thin config flip that enables a fallback arm —
e.g. `rebar.toml` — counts, even though it touches no error-handling *code*). If the plan
touches NONE of those semantics — a pure investigation / spike / doc-only / test-only plan, or a
non-failure use of a word like "fallback" (a default-value / config-contract sense, a
single-child bin-packing fallback, a fallback-endpoint *test*) — this is not-applicable → PASS.

THE DEFECT — a failure path is added or altered but the plan never states its DISPOSITION
CONTRACT: what the caller ends up seeing when that path fails. Incident 1c0d escaped because a
plan (bug 8fbd's config flip) enabled a fallback chain whose terminal arm degraded to an
uncredentialed / unreachable provider, and nothing forced the plan to state what surfaces when
that arm fails — so a *retryable* primary throttle was masked by the fallback's own
*non-retryable* failure. The productive review move is to require the DISPOSITION per affected
arm BEFORE the mechanism ships.

FIRE A FINDING when the plan adds/alters a failure path but its failure-disposition contract is
incomplete — one or both:
1. an affected failure arm has NO stated disposition — the plan does not say, using an explicit
   disposition word (retryable / transient vs fatal / permanent), what the caller surfaces when
   that arm fails; OR
2. [CONDITIONAL — only when the plan adds/alters a SECONDARY / fallback arm] a fallback chain is
   present but the plan does not state WHICH disposition wins when the fallback itself fails —
   it must preserve the most-recoverable leg, so a retryable primary is never masked by the
   fallback's own non-retryable failure (the 1c0d root cause, at plan time).

ACCEPT (PASS) when EITHER:
- the plan is not-applicable (the GATE does not open — no failure/timeout/exception/retry/
  fallback semantics are added or altered); OR
- for EACH affected arm the plan states its disposition with an explicit disposition word, AND
  (when a fallback chain exists) states the fallback-failure winner and that it preserves the
  most-recoverable leg. A labeled "Failure-disposition contract:" section is RECOMMENDED and is
  the coaching move, but an INLINE statement PASSES if it unambiguously carries a disposition
  word per affected arm.

GRANULARITY — one statement per FAILURE-HANDLING ARM the plan adds or alters (the fallback arm,
the retry policy, the exception-mapping site) — NOT one blanket line (vacuous green) and NOT one
per exception subclass (enumeration noise). When ≥2 arms compose (primary + fallback), that
composition is itself one "path" requiring the clause-(2) winner statement. Fold multiple angles
on one arm into a SINGLE finding.

REBAR VOCABULARY — this project surfaces disposition through concrete tokens; treat their
presence as evidence the plan is (or should be) reasoning about disposition: INDETERMINATE,
WAIT_AND_RETRY, NEEDS_INVESTIGATION, exit-11, `classify_llm_failure`, `should_fall_back`, plus
the generic retry / backoff / jitter / timeout / deadline / fallback / failover / circuit-breaker
terms. A rebar disposition contract typically reads like: "a Bedrock 429 stays retryable
(exit-11 / WAIT_AND_RETRY); if the Anthropic fallback's own auth fails, the chain preserves the
primary's retryable disposition — the fallback's non-retryable TypeError is never surfaced."

DISTINCT FROM T5b — do NOT double-report T5b's concern here. T5b asks whether error-handling /
failover EXISTS at all (is there retry / backoff / graceful-degradation; are errors surfaced not
swallowed). This criterion is narrower and orthogonal: GIVEN a failure path is added or altered,
is its DISPOSITION CONTRACT stated and disposition-preserving? A plan that adds a new external
call WITH retry/backoff/timeout AND an explicit per-arm disposition statement PASSES here (the
contract is stated) even though it is a new failure point. When you fire, cite the
disposition-contract gap SPECIFICALLY ("state which disposition surfaces when the fallback arm is
unavailable here") — never restate "add error handling."

This is a SINGLE-TURN plan-text judgment — reason over the plan's own text and its declared
file_impact; you are not grounding against the codebase here.

CHECKLIST SUB-ANSWERS (criterion-local):
- affects_failure_disposition {yes|no|insufficient} — the GATE: does the plan ADD or ALTER a
  failure/timeout/exception/retry/fallback/circuit-breaker path (from the plan text or the
  declared file_impact)? `no` (touches no such semantics, or only a non-failure sense of the
  vocabulary, or doc/test-only) → not-applicable → PASS.
- disposition_contract_stated {yes|no|insufficient} — only meaningful when gated in: does the
  plan state its failure-disposition contract — a disposition word (retryable/transient vs
  fatal/permanent) per affected arm, AND (when a fallback chain exists) the fallback-failure
  winner preserving the most-recoverable leg? A missing per-arm disposition, or a fallback chain
  with no stated winner, is `no` (a finding); a fully-stated contract is `yes` (PASS); an
  ambiguous case is `insufficient` (coach, do not assert).

ADVISORY: this criterion errs toward surfacing and coaches ("state, per affected arm, the
disposition it surfaces — and, for a fallback chain, which leg wins on fallback-failure — before
the mechanism ships"); it does NOT block a plan. Promotion to a blocking posture is a future
dogfood-gated `.rebar/criteria_routing.json` change per the advisory→blocking promotion gate in
docs/plan-review-gate.md (the standing recorder `criterion_effectiveness.py` auto-monitors this
criterion with zero per-criterion wiring).
