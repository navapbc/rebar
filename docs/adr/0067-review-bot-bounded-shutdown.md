# ADR 0067 — The review-bot shutdown is bounded: drain, then cancel, both under budgets sized below the container grace period

- **Status:** Accepted (epic `373f`; ticket `ab77`)
- **Date:** 2026-08-08

## Context

The review-bot is a long-running FastAPI service (`review_bot/app.py`) that acknowledges Gerrit
webhooks with `202` immediately and processes each review off an in-memory `asyncio.Queue` in a
background worker, alongside a backfill reconciler and a snapshot-cache janitor. A routine
autodeploy recreates the container, which delivers SIGTERM and then, after the compose
`stop_grace_period`, escalates to SIGKILL.

Two failure modes motivated a bounded, two-phase shutdown:

- A deploy landing **mid-review** (observed at 260–911s) that simply cancels the worker abandons
  the acknowledged (`202` "queued") webhook — the in-memory queue is lost on every restart — and a
  teardown-corrupted review fail-closed a `-1` that then suppressed the backfill reconciler.
- An **unbounded** `await task` at teardown can hang the whole shutdown with no upper bound when a
  task is slow to honor cancellation (a shielded region, a synchronous `finally`, or the orphaned
  OS thread of an `asyncio.to_thread` offload that cannot be force-cancelled).

## Decision

Shutdown is **bounded and two-phase** (`app.lifespan`'s `finally`):

1. **Drain** — `await asyncio.wait_for(queue.join(), timeout=shutdown_drain_seconds())` lets the
   in-flight review finish the already-queued events before anything is cancelled. Each review is
   itself wall-clock-bounded by `REVIEW_TIMEOUT_SECONDS`, so the drain window is sized to that
   per-review budget (`DEFAULT_SHUTDOWN_DRAIN_SECONDS`). An empty queue drains instantly, so a
   no-review deploy pays nothing; anything still queued when the window elapses is left for the
   backfill reconciler — **fail-safe, never fail-lose**.
2. **Cancel + await** — the tasks are cancelled and joined under a second, independent budget
   `shutdown_cancel_seconds()` (`DEFAULT_SHUTDOWN_CANCEL_SECONDS`). A well-behaved task cancels
   promptly; a task slow to honor cancellation is **abandoned** (the process is exiting anyway),
   and its in-flight store write is bounded independently by `event_append`'s per-git timeout.

Total shutdown therefore has an upper bound of `drain + cancel`. Both budgets are single-sourced in
`review_bot/config.py` (not `app.py`) so the test that asserts the container's `stop_grace_period`
is sized above them can read the values **without** importing the fastapi-laden `app` module (the
`reviewbot` extra is absent in the default test tier). The compose `stop_grace_period` (`1320s`)
is kept strictly above `drain + cancel` so the drain completes before Docker escalates SIGTERM →
SIGKILL — an invariant pinned by
`tests/unit/test_review_bot.py::test_reviewbot_stop_grace_period_covers_an_in_flight_store_write`.

## Consequences

- The budget rationale lives here; `review_bot/config.py` and `app.py` keep their invariant
  comments (the measured mid-review window, the fail-safe drain, the two-phase bound) and cite
  this ADR as their fuller durable home.
- Any change that removes a budget, makes the join unbounded, or lowers the container
  `stop_grace_period` below `drain + cancel` reopens this decision — it would restore one of the
  two failure modes above.

## Alternatives rejected

- **Cancel the worker immediately on SIGTERM (no drain).** Abandons acknowledged webhooks and can
  fail-close a corrupted `-1` that suppresses the reconciler; rejected.
- **Unbounded `await task` after cancel.** A single slow-to-cancel task hangs shutdown forever;
  rejected in favor of the bounded second phase.
