# ADR 0008: Convergent plan-edit re-review (full-run + deterministic rising floor)

- **Status:** Accepted
- **Context:** Epic *Convergent plan-edit re-review: full-run + deterministic rising
  floor* (`7d43-49c5-bc4d-4926`); children e344 (sidecar finding-text), 150b (Pass-2
  novelty sub-call + eval gate), ec89 (remediation-mode eligibility), cc5b (Pass-3
  rising-floor drop rule), this doc/test child (4cb9). Relates to the plan-review epic
  `5fd2-a7c2-0aec-48fa` and ADR 0002 (code-drift invalidation).

## Context

The plan-review gate's re-review of an **edited** plan did not converge. Each
remediation round could surface *new* issues in previously-clean criteria, expanding
scope every run and creating a non-terminating loop that drives cost, wall-clock, and
agent non-compliance. The agent fixes the surfaced findings, re-runs the review, and the
re-review raises *different* (often lower-stakes) findings — so the gate never goes green
and the loop never ends.

This is **distinct** from run-over-run reproducibility on fixed input (the LLM-as-judge
self-consistency problem): the plan legitimately changed, so the feedback *should* differ.
The precise problem we solve: a remediation re-review should **confirm the surfaced issues
were resolved without introducing different, lower-stakes issues that restart the loop** —
while never freezing a genuine, high-stakes defect that an edit introduces.

## Decision

A **rising-floor remediation re-review**: run the FULL criteria set every time (no skipped
criteria, no Pass-1 anchoring → full recall), and on a remediation re-review apply a
**deterministic Pass-3 floor** that drops only **novel, low-priority** findings.

A finding is dropped **iff** `novelty ≥ T_novel` AND `priority < floor`
(`priority = validity × impact`). Carryover findings (low novelty — they match a prior
finding) are enforced at the normal threshold and must still be resolved; a novel
*high-priority* finding is preserved (and may block).

### Invariants (the design's load-bearing properties)

1. **Pass-1 is unanchored.** Every applicable criterion runs and the finder never sees the
   prior findings, so the anchoring/sycophancy the research documents cannot freeze a
   high-stakes defect an edit introduces. Recall is full every round.
2. **Novelty is scored in a SEPARATE Pass-2 sub-call.** A distinct structured sub-call
   (its own `novelty` contract + prompt) ALONE receives the prior findings and answers
   factual *matches-prior* sub-answers. The existing verification sub-call (severity +
   validity) receives NO prior findings, so the `verify.py` independence invariant is
   enforced **structurally**, by construction — not by a prompt assertion.
3. **The drop decision is deterministic in Pass-3.** `novelty = 1 − mean(matches-prior
   sub-answers)` and the drop predicate `rising_floor_drop(priority, novelty, …)` are pure
   arithmetic — no LLM holistic severity anywhere in the decision path.
4. **Remediation mode is gated, not the default.** (`remediation_mode` and
   `novelty_drop_active` were retired in the config-prune epic; both behaviors are now
   always-on and unconditional — the configurable-toggle framing below is historical.) The
   floor applies only when ALL hold:
   config `remediation_mode` on, the plan changed, the **code is unchanged** since the
   baseline (`verified_at_sha` equality — reusing the signed snapshot ref, no new diff
   machinery), the registry is unchanged, a prior sidecar with finding text exists, and the
   last review is within the freshness window (reset on each review). Any precondition
   failing → a **byte-identical full review**. A separate evidence-gate flag
   (`novelty_drop_active`) completes the triple gate. (Update 2026-07-11: both flags now
   default ON — operator-authorized on field evidence in lieu of the `discriminates_novelty`
   eval; an explicit `false` on either is the back-out. As written at acceptance time, both
   shipped default-off pending that eval.)

### Within-session suppression (a deliberate, honest bound)

Because the window resets on each review, a novel low-priority finding can be re-floored
every round while the agent keeps iterating — within-session suppression is
**intentionally unbounded** (the chosen persistence behavior). It is bounded only by: the
idle-lapse window, carryover findings still being enforced at the normal bar, and every
dropped finding being recorded in the `REVIEW_RESULT` sidecar (joinable by `norm_id`), so
suppression is always observable. No unsound *coverage* accumulates — Pass-1 always runs
the full criteria set; only the *surfacing* of novel low-priority findings is suppressed.

## Alternatives rejected

- **Per-diff-region scoping** — re-review only the edited plan sections. Rejected: it needs
  new section-diffing machinery and, worse, it would MISS a high-stakes defect an edit
  introduces *outside* the diffed region (recall is not full). Our full-run Pass-1 keeps
  recall total.
- **Skip-clean-criteria reuse** — carry forward "no finding" for criteria that were clean
  last round. Rejected as **unsound**: it reuses an *absence of finding*, which freezes a
  high-stakes defect an edit introduces on a previously-clean criterion. The floor instead
  re-runs everything and suppresses only *novel low-priority* findings, never high-stakes
  ones.
- **The whole-verdict probe** — a single coarse "did this materially change?" check.
  Rejected as too coarse: it cannot distinguish a resolved carryover from a newly-surfaced
  low-stakes finding, so it neither converges reliably nor preserves high-stakes recall.

## Consequences

- The loop **converges to a signed PASS in bounded rounds** for a pure plan edit on
  unchanged code: each round drops the novel low-priority noise that would restart it,
  while carryover and novel high-priority findings still gate.
- **Back-out is trivial and total:** setting `remediation_mode` (or `novelty_drop_active`)
  off restores byte-identical full-review behavior. (Retired in the config-prune epic; these
  keys no longer exist and the behavior is now always-on.) The `drift_refresh` code-drift path
  (ADR 0002) is untouched and orthogonal (it is the *complement* on the material axis:
  drift-refresh = plan unchanged + code drifted; remediation = plan changed + code
  unchanged).
- **One of three Pass-3 floors.** This novelty floor is one of three deterministic Pass-3
  drop/refresh paths along independent axes: **novelty** (this ADR — plan-edit convergence),
  **material freshness** (ADR 0002 drift-refresh), and **delivered-completion** (ADR 0024 —
  the container completion floor). They compose; each is separately gated. (Update
  2026-07-11: the novelty floor's two flags now default ON, operator-authorized on field
  evidence; the other two floors remain inert by default.)
- Suppression is **observable**: narrowed verdicts record `narrowed` + `floored_criteria`
  + `floored_finding_ids` on coverage, and the dropped findings live in the sidecar.
