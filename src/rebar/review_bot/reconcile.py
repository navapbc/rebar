"""Backfill review votes lost with Gerrit webhooks.
Poll events-log for the project's current vote-less revisions and use the
same single-flight voter as webhooks. Persist an inclusive event-time
cursor. Hold it at the oldest retryable candidate, bounded by the
configured ceiling, so a failed review remains reachable. Vote and
dedup checks prevent duplicate votes. On events-log failure, keep the
cursor and vote state unchanged and emit RECONCILE_DEGRADED."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rebar._store.fsutil import atomic_write
from rebar.review_bot import voter as _voter
from rebar.review_bot.config import ReceiverConfig, review_timeout_seconds
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.reconcile")

#: events-log ``?t1=`` expects ``yyyy-MM-dd HH:mm:ss`` in UTC.
_T1_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Error and deferred results still owe a vote, so hold the cursor.
#: Completed votes, irrelevant events, existing votes, and closed changes
#: are terminal and let it advance.
_RETRYABLE_STATUSES = frozenset({"error", "deferred"})

#: A module-wide bool requests cooperative shutdown without binding to
#: one event loop. It stops new candidates and lets lifespan await the
#: current backfill review under the queue's drain deadline.
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
    """Emit one greppable RECONCILE_DEGRADED stderr marker plus a
    structured log body, without duplicating the marker."""
    record = {"event": "RECONCILE_DEGRADED", "timestamp": time.time(), "reason": reason, **fields}
    body = json.dumps(record, default=str)
    logger.warning(body)
    print("RECONCILE_DEGRADED " + body, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker


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
    """Atomically replace the cursor through a unique same-directory temp.
    Log write failures and let the next pass safely rescan."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, value, encoding="utf-8")
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
    """Keep each change's latest patchset-bearing event for this project.
    Carry eventCreatedOn for cursor holdback without affecting voter parsing."""
    latest: dict[str, dict] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        change = ev.get("change") or {}
        patchset = ev.get("patchSet") or ev.get("patchset") or {}
        if project and change.get("project") and change.get("project") != project:
            continue
        # Skip closed changes that cannot receive votes. Missing status
        # metadata proceeds so a votable change is not lost.
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
    """Review current vote-less candidates since the persisted cursor.
    Return scanned and reviewed counts. Hold the inclusive cursor at the
    oldest retryable abandonment, within the configured ceiling. On
    events-log failure, emit degradation and neither vote nor advance."""
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
    # Track event times of retryably unvoted candidates. Their minimum
    # keeps the next inclusive fetch window open.
    held_back: dict[str, int] = {}
    pending = list(candidates.items())
    for position, (change_id, ev) in enumerate(pending):
        # Stop taking candidates during shutdown and hold each unvoted
        # event behind the cursor. The next startup pass can retry them.
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
        # Bound backfill reviews by the worker's review timeout. A timeout
        # emits degradation, leaves the change vote-less, and remains retryable.
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
    """Persist and return the cursor's low-water mark.
    Hold at the oldest retryable event, capped behind the newest event by
    reconcile_max_holdback_seconds. Report an expired holdback."""
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
    """Run a pass at startup and each interval. Log pass failures and
    continue. On request_stop, finish work already in flight and start no
    new review so lifespan can drain under its shared deadline."""
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
