"""Backfill reconciler — recover patchsets a dropped webhook never voted (S4b).

Gerrit's ``events-log`` plugin is a STORE of events, not an auto-replay: if a webhook
delivery is lost (receiver restart, transient network) the patchset is never voted and
— because submit REQUIRES the vote — the change sits unsubmittable forever. This poller
closes that loop. On startup and every ``RECONCILE_INTERVAL_SECONDS`` it reads the
events-log, finds patchsets in the rebar project whose CURRENT revision has no
``LLM-Review`` vote, and re-invokes the SAME ``voter.review_and_vote`` — which shares
the single-flight lock + dedup, so a webhook and a backfill for the same patchset never
double-vote.

PERSISTED CURSOR WITH A LOW-WATER MARK (resumable, and re-drives what it abandoned).
The reconciler stores an events-log event time in a small file (``config.cursor_path`` —
by default ``<dedup dir>/reconcile_cursor``) and each pass fetches only events SINCE that
cursor (the events-log REST ``?t1=`` time window). That window is a SERVER-SIDE INCLUSIVE
LOWER BOUND: events older than ``t1`` are simply absent from the response. So the cursor is
NOT merely an optimization — anything the cursor moves past is gone from every future
window, permanently.

Bug 9f63: this pass used to advance the cursor to the newest event in the whole fetched
window UNCONDITIONALLY, including when a candidate had been abandoned mid-flight (an
events-log check error, a review timeout, or a voter ``error``/``deferred`` result). The
abandoned change fell out of every subsequent window and was never retried — the docstring's
promise that "this poller closes that loop" silently regressed, and only a new event
(a ``recheck-review`` comment, a re-push) could re-admit the change. The cursor is now a
LOW-WATER MARK: a pass persists ``min(newest_event, oldest_retryably_abandoned_candidate)``,
so the next pass re-fetches every candidate it still owes a vote. Terminal outcomes (voted,
already-voted, malformed, other-project, or a change closed mid-review) do NOT hold it back.
The hold-back is bounded by ``reconcile_max_holdback_seconds`` so a permanently-failing
candidate cannot pin the cursor — and forever grow the window — in silence; when that
ceiling releases one, the greppable ``RECONCILE_DEGRADED reason=holdback_expired`` marker
fires.

The cursor survives a restart (resumable). IDEMPOTENCY remains owned by the
per-(change,revision) dedup ledger + the authoritative Gerrit vote-existence check, so a
re-drive of an already-voted patchset — or even a lost/reset cursor — can never double-vote.

FALLBACK (fail-closed, degraded). If events-log is absent / errors / returns malformed
data, the reconciler logs a warning, emits a greppable ``RECONCILE_DEGRADED`` marker the
host probe / alarm can catch, and RELIES ON THE WEBHOOK (degraded backfill). It NEVER
advances the cursor on an error and NEVER casts a vote it could not justify — a missed
change simply stays vote-less = unsubmittable = fail-closed. Note the webhook is only a
BEST-EFFORT partner: Gerrit's ``webhooks`` plugin is at-most-once (it logs SEVERE and
DISCARDS after ``maxTries``, and loses pending deliveries on restart), so this reconciler
is the only recovery path — which is exactly why its cursor must not skip work (9f63).

The reconciler reuses one ``GerritClient`` + ``DedupStore`` so the run is cheap. For
each bounded events-log candidate it queries Gerrit FIRST. A definitive vote-less read
invalidates any stale local dedup row before the ordinary voter runs; a Gerrit read error
preserves local state and holds the cursor back for retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rebar.review_bot import voter as _voter
from rebar.review_bot.config import ReceiverConfig, review_timeout_seconds
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.reconcile")

#: events-log ``?t1=`` expects ``yyyy-MM-dd HH:mm:ss`` in UTC.
_T1_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The ``voter.review_and_vote`` result statuses that leave the patchset STILL OWED a vote and
#: are worth re-driving next pass — so the cursor is held back to them (bug 9f63).
#:
#: ``error`` covers every transient fail-closed stage the voter reports (``dedup_check``,
#: ``review_setup``, ``post_vote``, the tree-mismatch guard): none of them wrote a dedup row, so
#: a re-drive re-attempts. ``deferred`` is the retryable-coverage-gap path, whose own contract
#: (voter.py) explicitly says the reconciler will re-drive it before the attempt budget escalates
#: to the fail-closed ``-1``.
#:
#: Everything else is TERMINAL and must let the cursor advance: ``voted`` (done);
#: ``skipped``/``malformed_event`` and ``skipped``/``other_project`` (never ours to vote);
#: ``skipped``/``dedup`` and ``skipped``/``already_voted_gerrit`` (a vote already exists); and
#: ``skipped``/``post_vote_closed`` — the change merged/abandoned mid-review, so it is unvotable
#: and re-driving it would 409 forever (bug c943).
_RETRYABLE_STATUSES = frozenset({"error", "deferred"})

#: Cooperative-shutdown flag (ticket 9ec0). The app lifespan drains the webhook queue with
#: ``queue.join()``, which does NOT cover this module: ``reconcile_once`` awaits
#: ``review_and_vote`` INLINE, so with an empty queue the join returns at once and the
#: reconcile task is cancelled within ``shutdown_cancel_seconds()`` — killing a backfill
#: review that may be minutes in. Since the reconciler is the path that RETRIES a review lost
#: to anything else, that is the self-heal path being killed.
#:
#: The fix is cooperative rather than a second drain primitive: ``request_stop()`` makes the
#: loop stop taking NEW candidates and return as soon as the review in flight finishes, so
#: the reconcile TASK ITSELF becomes the thing the lifespan can await — under the same
#: single ``shutdown_drain_seconds()`` deadline as the queue drain.
#:
#: Deliberately a plain bool, not an ``asyncio.Event``: an Event binds to the loop it is first
#: awaited on, and this is module state that outlives any one loop, so a second
#: ``asyncio.run`` would hit "bound to a different event loop". A bool has no loop affinity.
_stop_requested = False

#: Granularity at which an IDLE reconciler (parked between passes) notices a stop request.
#: Small enough that shutting down an idle bot is not perceptibly delayed, large enough to be
#: free at steady state.
_STOP_POLL_SECONDS = 0.25


def request_stop() -> None:
    """Ask the reconcile loop to wind down: finish the review in flight, start no new one,
    and return. Idempotent; called by the app lifespan at the start of its shutdown drain."""
    global _stop_requested
    _stop_requested = True


def clear_stop() -> None:
    """Re-arm the loop (undo :func:`request_stop`). Called when a lifespan STARTS so a fresh
    app in a process that already ran one — a reload, or a test — is not born shutting down."""
    global _stop_requested
    _stop_requested = False


def stop_requested() -> bool:
    """Whether a cooperative shutdown has been requested."""
    return _stop_requested


async def _sleep_unless_stopping(seconds: float) -> None:
    """Sleep up to ``seconds``, returning EARLY once a stop has been requested, so an idle
    reconciler does not hold shutdown open for a whole reconcile interval."""
    remaining = max(0.0, seconds)
    while remaining > 0 and not _stop_requested:
        step = min(_STOP_POLL_SECONDS, remaining)
        await asyncio.sleep(step)
        remaining -= step


def _emit(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, "timestamp": time.time(), **fields}, default=str))


def _degraded(reason: str, **fields: Any) -> None:
    """Emit the greppable ``RECONCILE_DEGRADED`` marker (to stderr/journald too) so the
    host observability probe / alarm sees that backfill is degraded and the pipe is
    relying on the webhook alone. Mirrors voter's ``VOTER_ERROR`` marker convention."""
    record = {"event": "RECONCILE_DEGRADED", "timestamp": time.time(), "reason": reason, **fields}
    line = "RECONCILE_DEGRADED " + json.dumps(record, default=str)
    logger.warning(line)
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker


def _read_cursor(path: str) -> str | None:
    """Read the persisted cursor (an events-log ``t1`` timestamp string), or ``None`` if
    there is no cursor yet (first run) / it is unreadable (treated as no cursor → full
    scan; the dedup ledger still prevents double-votes)."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return raw or None


def _write_cursor(path: str, value: str) -> None:
    """Persist the cursor atomically (write-temp + replace) so a crash mid-write can
    never leave a truncated cursor. Best-effort: a write failure is logged, not fatal —
    the next pass just rescans a little more (still idempotent)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        _emit("reconcile_cursor_write_error", error=str(exc), path=path)


def _event_time(ev: dict) -> int:
    """The event's creation time (epoch seconds). Gerrit events-log carries
    ``eventCreatedOn`` (epoch seconds); fall back to ``patchSet.createdOn`` / 0."""
    for key in ("eventCreatedOn",):
        try:
            v = int(ev.get(key) or 0)
            if v:
                return v
        except (TypeError, ValueError):
            continue
    patchset = ev.get("patchSet") or ev.get("patchset") or {}
    try:
        return int(patchset.get("createdOn") or 0)
    except (TypeError, ValueError):
        return 0


def _to_t1(epoch_seconds: int) -> str:
    """Render an epoch-seconds time as the events-log ``?t1=`` string (UTC)."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(_T1_FORMAT)


def _candidate_events(events: list[dict], project: str) -> dict[str, dict]:
    """Reduce the events-log to one ``patchset-created``-shaped event per change,
    keeping the LATEST patchset seen (highest patchSet.number) for the rebar project.

    The events-log mixes many event types; we keep the patchset-bearing ones so the
    voter's ``_extract`` can pull change/revision/ref. Keyed by change id.

    The normalized event CARRIES ITS ``eventCreatedOn`` FORWARD (bug 9f63): the cursor's
    low-water mark needs each candidate's own event time to clamp back to, and the voter's
    ``_extract`` reads only ``change``/``patchSet``/``type``, so the extra key is inert there."""
    latest: dict[str, dict] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        change = ev.get("change") or {}
        patchset = ev.get("patchSet") or ev.get("patchset") or {}
        if project and change.get("project") and change.get("project") != project:
            continue
        # Skip changes Gerrit considers CLOSED: voting a MERGED/ABANDONED change draws a 409
        # "change is closed" that the voter records as a non-actionable voter_error and — no
        # dedup row is written on failure — re-attempts forever (bug c943). Only an open
        # change is votable. Fail OPEN on an absent/unknown status: never drop a candidate on
        # missing metadata (that would risk skipping a live open change and stalling the gate).
        status = change.get("status")
        if status and str(status).upper() != "NEW":
            continue
        change_id = change.get("id")
        if not change_id or not patchset.get("revision") or not patchset.get("ref"):
            continue
        try:
            num = int(patchset.get("number") or 0)
        except (TypeError, ValueError):
            num = 0
        prev = latest.get(str(change_id))
        prev_num = 0
        if prev is not None:
            prev_ps = prev.get("patchSet") or prev.get("patchset") or {}
            try:
                prev_num = int(prev_ps.get("number") or 0)
            except (TypeError, ValueError):
                prev_num = 0
        if prev is None or num >= prev_num:
            # Normalize to a patchset-created-shaped event the voter understands.
            latest[str(change_id)] = {
                "type": "patchset-created",
                "change": change,
                "patchSet": patchset,
                "eventCreatedOn": _event_time(ev),
            }
    return latest


async def reconcile_once(
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> dict[str, int]:
    """Run one backfill pass. Returns ``{scanned, reviewed}`` counts for observability.

    Reads events SINCE the persisted cursor, reviews gap (vote-less) patchsets, then persists
    the cursor as a LOW-WATER MARK: the newest event seen, but never past the OLDEST candidate
    this pass abandoned for a retryable reason (bug 9f63), because the events-log ``?t1=``
    window is a server-side inclusive lower bound and anything the cursor passes is
    unreachable forever. The hold-back is clamped to ``reconcile_max_holdback_seconds`` behind
    the newest event so a permanently-failing candidate cannot pin the cursor.

    On an events-log error/malformed body it emits the degraded marker, does NOT advance the
    cursor, and casts NO vote (fail-closed; the webhook remains the live path)."""
    cfg = config or ReceiverConfig.from_env()
    gc = gerrit or GerritClient(cfg)
    store = dedup or DedupStore(cfg.dedup_db_path)
    cursor = _read_cursor(cfg.cursor_path)

    try:
        events = await asyncio.to_thread(gc.list_events, cursor)
    except GerritError as exc:
        # events-log absent / errored → degraded: rely on the webhook, never advance the
        # cursor, cast nothing. The change stays vote-less = unsubmittable (fail-closed).
        _degraded("events_log_error", error=str(exc), http_status=getattr(exc, "status", None))
        return {"scanned": 0, "reviewed": 0}

    if not isinstance(events, list):
        # Malformed body (not a list of events) → degraded; do not advance, cast nothing.
        _degraded("events_log_malformed", body_type=type(events).__name__)
        return {"scanned": 0, "reviewed": 0}

    candidates = _candidate_events(events, cfg.project)
    # The newest event time across the WHOLE fetched window (not just candidates), so the
    # cursor advances past comment-added/etc. events too and the next pass fetches a
    # smaller tail.
    newest = 0
    for ev in events:
        if isinstance(ev, dict):
            newest = max(newest, _event_time(ev))

    reviewed = 0
    # change_id -> the candidate's own event time, for every candidate this pass leaves
    # un-voted for a RETRYABLE reason. Its minimum is the cursor's low-water mark (9f63): the
    # next pass's ``?t1=`` window must still contain these, or they are lost for good.
    held_back: dict[str, int] = {}
    pending = list(candidates.items())
    for position, (change_id, ev) in enumerate(pending):
        # Cooperative shutdown (ticket 9ec0): once shutdown has begun, take NO new candidate.
        # This is what bounds the drain — the lifespan waits for the review in flight, and a
        # pass that kept picking up fresh candidates could re-extend that wait up to the whole
        # budget, candidate after candidate.
        #
        # Every candidate from here on is left un-voted, so each is HELD BACK (bug 9f63) rather
        # than merely skipped. The cursor is a LOW-WATER MARK: without the hold-back it would
        # still advance to ``newest``, pushing these candidates out of the next pass's inclusive
        # ``?t1=`` window — unreachable forever. Shutdown must not become the one path that
        # silently drops a gap patchset; held back, they stay vote-less (fail-closed) and the
        # next container's startup pass picks them straight back up.
        if _stop_requested:
            for skipped_id, skipped_ev in pending[position:]:
                held_back[skipped_id] = _event_time(skipped_ev)
            _emit(
                "reconcile_stopping",
                skipped_from=change_id,
                skipped=len(pending) - position,
                reviewed=reviewed,
            )
            break
        patchset = ev.get("patchSet") or {}
        revision = str(patchset.get("revision"))
        try:
            ev_time = int(ev.get("eventCreatedOn") or 0)
        except (TypeError, ValueError):
            ev_time = 0
        # Gerrit is authoritative and MUST be checked before local dedup. A neutral reset
        # deliberately leaves the old SQLite row behind; the reset-emitted event reaches
        # this candidate and a definitive no-vote read invalidates only that stale row.
        try:
            if await asyncio.to_thread(gc.has_llm_review_vote, change_id, revision):
                continue
        except GerritError as exc:
            # We could not even establish whether a vote is needed — the candidate is still
            # OWED one. Hold the cursor back to it so the next pass re-checks (9f63).
            _emit(
                "reconcile_check_error",
                change_id=change_id,
                revision_id=revision,
                error=str(exc),
            )
            held_back[change_id] = ev_time
            continue
        store.clear_voted(change_id, revision)
        # Bound the backfill review with the SAME per-review timeout the live worker uses
        # (app._worker). Without this a single hung review (blocked clone/subprocess/LLM) would
        # freeze the ENTIRE reconcile loop indefinitely — the backfill safety-net having no
        # safety-net of its own. On timeout: abandon this candidate, emit the greppable degraded
        # marker (stderr + metric), and continue; the change stays vote-less (fail-closed) and
        # is retried next pass.
        try:
            result = await asyncio.wait_for(
                _voter.review_and_vote(ev, config=cfg, gerrit=gc, dedup=store),
                timeout=review_timeout_seconds(),
            )
        except (asyncio.TimeoutError, TimeoutError):
            _degraded("review_timeout", change_id=change_id, revision=revision)
            held_back[change_id] = ev_time
            continue
        status = str(result.get("status") or "")
        if status == "voted":
            reviewed += 1
        elif status in _RETRYABLE_STATUSES:
            # ``error`` (transient, no dedup row written) or ``deferred`` (retryable coverage
            # gap): the patchset is still vote-less, so keep it inside the next window (9f63).
            held_back[change_id] = ev_time

    persisted = _persist_cursor(cfg, newest=newest, held_back=held_back, previous=cursor)

    _emit(
        "reconcile_done",
        scanned=len(candidates),
        reviewed=reviewed,
        cursor_advanced=bool(newest),
        held_back=len(held_back),
        cursor=persisted,
    )
    return {"scanned": len(candidates), "reviewed": reviewed}


def _persist_cursor(
    cfg: ReceiverConfig, *, newest: int, held_back: dict[str, int], previous: str | None
) -> str:
    """Compute + persist this pass's cursor and return the value actually on disk.

    The cursor is a LOW-WATER MARK (bug 9f63), not simply "the newest event seen": it is
    clamped back to the OLDEST retryably-abandoned candidate so the next pass's inclusive
    ``?t1=`` window still contains it. ``t1`` is inclusive, so clamping TO that event time
    (not before it) is enough to re-admit it.

    The clamp is BOUNDED by ``cfg.reconcile_max_holdback_seconds``: a candidate that fails on
    every pass would otherwise pin the cursor forever and grow the fetch window without bound.
    When the ceiling releases a still-failing candidate we emit the greppable degraded marker
    so the change is surfaced loudly instead of being dropped in silence."""
    if not newest:
        # Nothing datable in this window (empty log / all events time-less) — leave whatever
        # cursor is already on disk alone, exactly as before.
        return previous or ""

    target = newest
    # Ignore time-less (0) candidates: they carry no usable bound, and clamping to 0 would
    # rescan the entire retained log every pass.
    floors = [t for t in held_back.values() if t > 0]
    if floors:
        oldest = min(floors)
        ceiling = newest - max(0, cfg.reconcile_max_holdback_seconds)
        if oldest < ceiling:
            _degraded(
                "holdback_expired",
                change_ids=sorted(cid for cid, t in held_back.items() if 0 < t < ceiling),
                oldest_held_back=_to_t1(oldest),
                max_holdback_seconds=cfg.reconcile_max_holdback_seconds,
            )
            target = min(newest, ceiling)
        else:
            target = min(newest, oldest)

    value = _to_t1(target)
    _write_cursor(cfg.cursor_path, value)
    return value


async def reconcile_loop(
    interval: int | None = None,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> None:
    """Run ``reconcile_once`` on startup and then every ``interval`` seconds (default
    ``RECONCILE_INTERVAL_SECONDS``). Runs until cancelled (the app lifespan owns it);
    a per-pass failure is logged and the loop continues.

    Also returns cleanly once :func:`request_stop` has been called (ticket 9ec0). That is what
    makes this coroutine awaitable as a DRAIN by the app lifespan: the current pass finishes
    the review it already has in flight, declines to start another, and the task completes —
    so the lifespan can wait for the reconciler's inline review under the same
    ``shutdown_drain_seconds()`` deadline it gives the webhook queue, instead of cancelling
    that review outright within ``shutdown_cancel_seconds()``."""
    cfg = config or ReceiverConfig.from_env()
    every = interval if interval is not None else cfg.reconcile_interval_seconds
    gc = gerrit or GerritClient(cfg)
    store = dedup or DedupStore(cfg.dedup_db_path)
    while True:
        try:
            await reconcile_once(config=cfg, gerrit=gc, dedup=store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a pass must never kill the loop
            _emit("reconcile_loop_error", error=str(exc))
        # Checked AFTER the pass, so a stop requested mid-review still lets that review land.
        if _stop_requested:
            _emit("reconcile_loop_stopped")
            return
        await _sleep_unless_stopping(max(1, every))
        if _stop_requested:
            _emit("reconcile_loop_stopped")
            return
