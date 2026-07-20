# ADR 0052 — Plan-review validation consistency + the re-review-stability contract

**Status:** Accepted
**Date:** 2026-07-19
**Bug:** `5e40-0fe4-a966-425e` (`chordal-quasilegal-pipit`).
**Relates to:** ADR 0043 (operator-attested completion evidence) and the impact redesign in
closed epic `4cc3-23c9-b062-41ac` / story `0858-facb-6901-4781` (the hard-override amplifier).

## Context

The plan-review gate re-reviews an already-signed plan whenever its attestation reads stale.
Bug 5e40 documented a re-review that flipped **PASS → BLOCK** with **no plan change** (only
`main` advanced), never converged, and could only be claimed with `--force`. The triage
attributed each invalid finding to the pipeline stage that should have filtered it — finding
discovery, **validation assessment**, or impact assessment — and surfaced two structural
anti-patterns worth recording so future gate work does not reintroduce them.

The whole-HEAD-invalidation TRIGGER that fires these re-reviews is deliberately untouched (an
earlier "stop invalidating on drift" fix was rejected as a security regression — see ADR-adjacent
notes on the drift floor). What 5e40 fixes is the re-review **OUTCOME**: convergent, deterministic
drops applied AFTER the verdict is produced (the Pass-3 floors and, now, the validation
cross-checks), not a loosening of the trigger.

## Decision

### 1. The remediation-cascade anti-pattern (a gate MUST NOT manufacture a block from an advisory)

Acting on an **advisory** finding must never, by itself, produce a **new blocking** finding on
the next round. In 5e40 the reviewer advised "also name `setup-project.sh` in Scope"; adding that
one sentence made the next round BLOCK because the newly-named deliverable had no AC and no
proving command. Each remediation widened the surface faster than it closed it, so the finding
count *rose* every round — the gate could not converge.

**Rule.** A round-over-round finding whose existence is *caused by* the previous round's
remediation of a lower-tier finding is not a fresh defect; it is cascade noise. The gate's
convergence mechanisms (the novelty/drift/completion Pass-3 floors, and the validation
cross-checks below) exist precisely so that re-review is **monotone-non-increasing** in the block
set for an unchanged-or-remediated plan. Escalating a secondary, newly-named audit-trail touch to
the blocking tier is impact inflation and is explicitly disallowed.

### 2. The re-review-stability contract

Re-running `review-plan` on an **unchanged, previously-signed** plan — even at a **drifted HEAD**
— MUST yield a verdict with **no NEW blocking findings** relative to the signing review. A drifted
HEAD may legitimately surface a finding that cites the code that changed; it must not mint blocks
on byte-identical plan text that cite unrelated code, restate a demonstrably-present fix, or
re-litigate a settled point. This is the headline contract the Pass-3 drift floor (already landed)
and the two validation cross-checks below jointly uphold.

### 3. Validation assessment cross-checks two consistency axes (this change)

The "deciding whether a candidate is real" stage now runs two deterministic drops, each a PURE
apply function over the verdict's surfaced findings (unit-testable with an injected judgment) plus
a gated sub-call that mirrors the novelty/completion sub-call pattern:

- **Intra-verdict contradiction.** When one verdict both asserts a thing ABSENT (a blocking "no
  one is tasked with capturing the snapshot") and, in another finding, asserts it PRESENT (an
  advisory "the parent explicitly assigns capture to S1"), the two cannot both be true. The
  contradicted/false member is dropped (`drop_reason="contradiction"`). The deterministic drop
  math (`decide.contradiction_drop_index`) follows the model-identified false member, falling back
  to dropping the lower-priority ("weaker") one — so a *false BLOCK* refuted by a *true advisory*
  is the one removed, not the true advisory.
- **Comment-trail consultation.** A finding that re-litigates a point the ticket's recorded comment
  trail already resolved or conceded (5e40 B3: `rebase:chain` was fact-checked and conceded in a
  prior round) is dropped (`drop_reason="comment_trail"`).

Both are gated inert behind evidence-gate config flags (`verify.contradiction_xcheck_active`,
`verify.comment_trail_xcheck_active`, default `False`), exactly like `completion_floor_active`: the
mechanism ships off; an operator enables it only after calibration confirms it never suppresses a
real finding. When inert the verdict is byte-identical.

## Consequences

- Re-review of an unchanged signed plan converges; the false positives catalogued in 5e40 (A1 the
  self-contradiction, B3 the conceded point) are droppable deterministically with a recorded
  `drop_reason` in the sidecar `dropped[]` bucket (full audit trail).
- Every drop is **fail-safe toward KEEP**: a malformed judgment, a failed sub-call, an unreadable
  config, or an ambiguous answer drops nothing. A broken signal can only make the gate stricter,
  never suppress a genuine finding.
- The convergence lives in the OUTCOME layer; the whole-HEAD-invalidation trigger and the impact
  hard-override (story `0858`) are unchanged. Fixing validation mis-tagging upstream remains the
  higher-leverage lever for the 0.85 hard-override escalations, tracked separately.
