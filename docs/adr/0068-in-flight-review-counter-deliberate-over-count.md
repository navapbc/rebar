# ADR 0068 — The in-flight review counter deliberately over-counts; the deploy loop must never kill a running review

- **Status:** Accepted (epic `373f`; ticket `ab77`)
- **Date:** 2026-08-08

## Context

A container recreation mid-review is **invisible** to every health signal the box has: the process
was asked to stop, so nothing fails and no `VOTER_ERROR` is emitted. A landing burst can therefore
live-lock the LLM-Review gate — killing review after review — with `restarts=0` and all alarms
green. To let the deploy loop (`infra/scripts/autodeploy.sh`) **defer** a recreation that would
kill work, the review-bot exports a busy-signal: `in_flight_reviews()` over `/health`
(`review_bot/voter.py`, bug `34cd`).

The counter is a plain module-level int (`_in_flight`) held up by the `_counting_in_flight()`
context manager for the duration of one review. No lock is needed: every mutation happens on the
asyncio event-loop thread (the increment/decrement bracket the coroutine's own body, and the
blocking work inside is offloaded with `asyncio.to_thread` while the count stays held by the
coroutine), and `/health` is served on that same loop, so a reader never observes a torn value.

## Decision

The count brackets the **whole** of `review_and_vote` — including its cheap dedup / already-voted
short-circuits — not only the expensive clone+LLM region, and it covers **both** review paths (the
webhook queue worker **and** the backfill reconciler), because both funnel through
`review_and_vote`.

Both choices are deliberate and follow from an **asymmetric** error cost:

- **Over-counting** (holding the count up across a cheap skip) delays a deploy by one ~2-minute
  timer tick — cheap and self-correcting.
- **Under-counting** (dropping the count while real review work runs) lets the deploy loop kill a
  ~10-minute review — expensive and exactly the live-lock the signal exists to prevent.

So the counter **biases toward over-counting**, and its coverage of the reconciler path is the
point: the reconciler's inline backfill review is the path that *retries* a killed review, so a
busy-signal blind to it would let the deploy loop keep killing the very work that is supposed to
heal the gate.

## Consequences

- The deliberate over-count and the both-paths coverage are load-bearing correctness properties,
  not incidental. `review_bot/voter.py` keeps its inline explanation (with the measured timer-tick
  vs review-duration asymmetry) and cites this ADR.
- Any change that narrows the bracket to only the expensive region, drops the reconciler path from
  the count, or "optimizes away" the over-count on cheap skips reopens this decision — it would
  reintroduce the invisible-kill live-lock.
- The no-lock reasoning is valid only while every mutation stays on the event-loop thread; moving
  any increment/decrement off that thread would require a lock and reopens this ADR.

## Alternatives rejected

- **Count only the clone+LLM region.** Under-counts during dedup/setup and can drop the signal at
  the moment a deploy is deciding whether to defer; rejected.
- **Count only the webhook path.** Blinds the signal to the reconciler's retry path — the deploy
  loop would keep killing the healing work; rejected.
