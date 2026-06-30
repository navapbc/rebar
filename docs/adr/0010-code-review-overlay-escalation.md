# ADR 0010: Code-review base→overlay escalation (one-hop, capped, recall-only)

- **Status:** Accepted
- **Context:** Epic *Agentic code-review capability* (`b744-4fb9-2d05-4b49` /
  dowdy-swear-bird), story WS3 (`8855-e5f9-05b4-4a04` / crow-knelt-fiber). Builds on the
  shipped review kernel (`docs/review-kernel.md`) and mirrors the plan-review gate. Relates
  to ADR 0003 (workflow-as-gate).

## Context

The code-review gate reviews a diff with a single base reviewer plus a catalog of 11
specialist OVERLAYS (security, performance, db-migrations, …). Running all 11 on every
change is wasteful; running only a fixed glob-triggered set misses concerns the change
implies but whose files don't obviously match a glob (e.g. a refactor that quietly weakens
an auth check). We want the recall of "run the specialists that matter" without (a) letting
an LLM's recommendation balloon cost or (b) letting it influence the gate DECISION.

## Decision

A **two-round, one-hop, capped overlay escalation on the RECALL side only**:

1. **Round-A (deterministic).** `overlay_union` computes the glob-triggered set — the
   overlays whose `applies_to` globs (in `code_review/criteria_routing.json`) match the
   diff's changed files. These run first.
2. **Base reviewer.** Always runs; emits Pass-1 findings AND a bounded, enum-constrained
   `recommend_overlays` signal (each id validated against the closed `OVERLAY_IDS`; unknown
   ids are dropped, not errored).
3. **Round-B (agent-escalated).** `overlay_union` computes `(glob ∪ base.recommend) −
   already_run`, ordered by `OVERLAY_IDS`, then **capped at N=3** (configurable in
   `gates/code-review.yaml`, not hard-coded). These are the freshly-escalated overlays.
4. All findings (base + Round-A + Round-B) flow into the SAME kernel Pass-2 verify → Pass-3
   decide → Pass-4 coach.

**Invariants:**

- **One-hop.** Round-B overlays emit `code_review_findings` only (no `recommend_overlays`
  field), and `overlay_union` runs exactly twice (triggers, union) — so an escalated overlay
  can never itself trigger a third round. The `already_run` set bars Round-A members from
  re-running.
- **Capped.** Round-B is bounded to N (default 3), so a runaway base reviewer cannot fan out
  to all 11 overlays. (N starts conservative per the orchestrator-worker "3-5 subagents"
  prior art; calibration is deferred.)
- **Recall-only — escalation can NEVER change the verdict.** `overlay_union` produces only
  membership flags (which overlays run); it is NOT an input to Pass-3. The decision is a pure
  function of `(findings, verifications, threshold_for)` in `code_review_decide`. A false or
  flipped escalation costs at most one extra advisory pass, never a different verdict for an
  identical finding set. This is pinned by an offline test
  (`test_code_review_workflow.py`): flipping `recommend_overlays` changes Round-B membership
  but not the verdict.

## Why not the alternatives

- **Single round (final union after base).** Loses the deterministic-vs-escalated
  distinction and the ability to prove escalation never reached the decision; the two-round
  shape makes the recall/precision split observable and testable.
- **Let the base reviewer's recommendation feed the decision (e.g. raise severity).** Would
  put an unbounded LLM signal on the precision side — exactly what the recall-only invariant
  forbids.
- **No cap.** A miscalibrated or adversarial base reviewer could fan out to every overlay,
  blowing the token budget; the cap bounds the blast radius.

## Gate config schema (`gates/code-review.yaml` operator-facing knobs)

The gate's operator-facing configuration is the `union` step's escalation cap; per-criterion
thresholds/posture are NOT in the gate YAML (they live in `criteria_routing.json`, read by
`registry.threshold_for`). The full config surface:

| Field | Location | Type | Default | Valid range | Meaning |
|-------|----------|------|---------|-------------|---------|
| `cap` | `gates/code-review.yaml` → `union` step `with.cap` | integer | `3` | `>= 0` (0 = no Round-B escalation; omit/`null` = uncapped) | Max Round-B escalated overlays (bounds agent-driven fan-out). |
| `block_threshold` | `criteria_routing.json[<criterion>]` | float | `0.95` | `0.0`–`1.0` | Pass-3 block threshold per criterion (min over a finding's criteria). |
| `blocking_enabled` | `criteria_routing.json[<criterion>]` | bool | `false` | — | Whether a criterion can block (true only for the WS5 secrets/security keys). |
| `applies_to` | `criteria_routing.json[<criterion>]` | list[str] (globs) | `[]` | — | Round-A glob triggers for an overlay (empty = escalation-only). |

The `cap` is a literal in the gate YAML (so calibrating it is a config edit, not a code change);
`overlay_union` reads it from its `with.cap` input. Non-int / negative / absent `cap` → uncapped
(documented in `overlay_union`'s contract).

## Consequences

- `overlay_union` + `merge_findings` are the only NEW scripted ops; the rest of the gate is
  kernel consumption (no forked passes). No core workflow-engine change.
- The cap and per-criterion thresholds live in config (`gates/code-review.yaml` /
  `criteria_routing.json`), so calibration is a config change, not a code change.
- Because Round-B membership depends on the base reviewer's output, the offline gate test
  uses a structured-output-capable fake runner to drive `recommend_overlays`.
