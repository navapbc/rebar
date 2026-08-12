# ADR 0069 — A retryable coverage gap defers vote-less under a bounded per-revision budget, then escalates to a fail-closed −1

- **Status:** Accepted (epic `373f`; ticket `ab77`)
- **Date:** 2026-08-08

## Context

Not every review failure is a code finding. A **coverage gap** is an infra failure that prevented
the review from running (a clone/fetch/context-assembly failure). Some gaps are transient and
worth retrying; failing them closed immediately would block a mergeable change on a blip, while
never failing them closed would let a change that can never be reviewed sit un-voted forever.

The review-bot distinguishes the two with the adapter's machine-readable `gap_reason`
(`review_bot/voter.py`, ticket `0347`): a real finding and an indeterminate-that-ran-to-completion
carry a **non-retryable** reason, and the merge-path `_merge_coverage_gap_decision` carries no
`gap_reason` at all — all of those still vote. Only a reason in `adapter.RETRYABLE_GAP_REASONS`
is eligible to defer.

## Decision

On a retryable coverage gap the voter **defers**: it casts **no** vote, so the vote-less change
stays visible to the backfill reconciler, which re-drives it within its interval. Deferral must be
**invisible to Gerrit entirely** — nothing (not even a provisional comment) is posted, because the
reconciler pre-filters on "no LLM-Review vote"; a provisional comment would leave the change
vote-less yet perturb that filter.

Deferral is **bounded**. Each attempt is counted per `(change_id, revision)` in the receiver's
`review_attempts` ledger (`dedup.py`). While `attempts < retryable_gap_max_attempts`
(`DEFAULT_RETRYABLE_GAP_MAX_ATTEMPTS`) the voter emits `REVIEW_RETRY_DEFERRED` and returns
`deferred`. Once the budget is spent it **escalates**: it casts the fail-closed `−1` (the message
gains an exhausted note; the first-line tag vocabulary is unchanged) and fires the `VOTER_ERROR`
marker so the `voter_errors` alarm surface sees the escalation. A poison-pill candidate therefore
cannot defer forever — it converges to a definite fail-closed vote.

The attempt budget is **shared** with the bb9b contributor re-trigger, whose explicit
`reset_attempts` re-arms it, so a human re-push / `rerun-llm-review` gives a genuinely transient gap
a fresh budget. The counter itself **fails open** (`record_attempt` errors are swallowed with a
`VOTER_ERROR` and treated as attempt 0) — an uncounted attempt only delays escalation by one
reconcile cycle; the fail-open is on the **counter**, never on the vote.

This deferral cooperates with the reconciler's bounded cursor hold-back
(`reconcile.py`, bug `9f63`, ceiling `reconcile_max_holdback_seconds`): the reconciler treats
`deferred` as a retryable status and holds its cursor back to re-drive the change, but that
hold-back is itself bounded so a permanently-failing candidate cannot pin the cursor forever.

## Consequences

- The defer→escalate contract, its bounded budget, and the reset seam live here; `voter.py` keeps
  its inline explanation (including the strict keying on `gap_reason` and the fail-open-on-counter
  note) and cites this ADR.
- Any change that removes the attempt bound, posts something to Gerrit on the deferred path, or
  moves the fail-open from the counter onto the vote reopens this decision.

## Alternatives rejected

- **Fail closed immediately on any coverage gap.** Blocks mergeable changes on a transient blip;
  rejected in favor of bounded retry.
- **Defer forever while the gap persists.** A poison-pill change would sit un-voted indefinitely;
  rejected — the budget forces convergence to a fail-closed `−1`.
- **Post a provisional comment on defer.** Perturbs the reconciler's "no vote" pre-filter and makes
  the deferral visible to Gerrit; rejected — deferral is silent.
