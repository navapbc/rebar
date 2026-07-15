"""Backfill reconciler — recover patchsets a dropped webhook never voted (S4b).

Gerrit's ``events-log`` plugin is a STORE of events, not an auto-replay: if a webhook
delivery is lost (receiver restart, transient network) the patchset is never voted and
— because submit REQUIRES the vote — the change sits unsubmittable forever. This poller
closes that loop. On startup and every ``RECONCILE_INTERVAL_SECONDS`` it reads the
events-log, finds patchsets in the rebar project whose CURRENT revision has no
``LLM-Review`` vote, and re-invokes the SAME ``voter.review_and_vote`` — which shares
the single-flight lock + dedup, so a webhook and a backfill for the same patchset never
double-vote.

PERSISTED CURSOR (resumable). The reconciler stores the newest events-log event time it
has processed in a small file (``config.cursor_path`` — by default
``<dedup dir>/reconcile_cursor``). Each pass fetches only events SINCE that cursor (the
events-log REST ``?t1=`` time window), then advances + persists the cursor to the newest
event seen. This survives a restart (resumable) and avoids rescanning the whole log; it
is purely an optimization — IDEMPOTENCY is still owned by the per-(change,revision) dedup
ledger + the authoritative Gerrit vote-existence check, so even a lost/reset cursor can
never double-vote.

FALLBACK (fail-closed, degraded). If events-log is absent / errors / returns malformed
data, the reconciler logs a warning, emits a greppable ``RECONCILE_DEGRADED`` marker the
host probe / alarm can catch, and RELIES ON THE WEBHOOK (degraded backfill). It NEVER
advances the cursor on an error and NEVER casts a vote it could not justify — a missed
change simply stays vote-less = unsubmittable = fail-closed.

The reconciler reuses one ``GerritClient`` + ``DedupStore`` so the run is cheap; the
per-patchset Gerrit-side ``has_llm_review_vote`` check is done inside the voter (the
authoritative skip), but we also pre-filter here to avoid spawning needless reviews.
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
    voter's ``_extract`` can pull change/revision/ref. Keyed by change id."""
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
            }
    return latest


async def reconcile_once(
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> dict[str, int]:
    """Run one backfill pass. Returns ``{scanned, reviewed}`` counts for observability.

    Reads events SINCE the persisted cursor, reviews gap (vote-less) patchsets, then
    advances + persists the cursor to the newest event seen. On an events-log
    error/malformed body it emits the degraded marker, does NOT advance the cursor, and
    casts NO vote (fail-closed; the webhook remains the live path)."""
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
    for change_id, ev in candidates.items():
        patchset = ev.get("patchSet") or {}
        revision = str(patchset.get("revision"))
        # Pre-filter: skip if locally recorded OR already voted on Gerrit (cheap skip
        # before spawning a review). The voter re-checks under the lock authoritatively.
        if store.already_voted(change_id, revision):
            continue
        try:
            if await asyncio.to_thread(gc.has_llm_review_vote, change_id, revision):
                continue
        except GerritError as exc:
            _emit("reconcile_check_error", change_id=change_id, error=str(exc))
            continue
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
            continue
        if result.get("status") == "voted":
            reviewed += 1

    # Advance + persist the cursor ONLY after a clean pass (newest event time seen). This
    # makes the poller resumable across restarts and avoids rescanning the whole log.
    if newest:
        _write_cursor(cfg.cursor_path, _to_t1(newest))

    _emit(
        "reconcile_done",
        scanned=len(candidates),
        reviewed=reviewed,
        cursor_advanced=bool(newest),
    )
    return {"scanned": len(candidates), "reviewed": reviewed}


async def reconcile_loop(
    interval: int | None = None,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> None:
    """Run ``reconcile_once`` on startup and then every ``interval`` seconds (default
    ``RECONCILE_INTERVAL_SECONDS``). Runs until cancelled (the app lifespan owns it);
    a per-pass failure is logged and the loop continues."""
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
        await asyncio.sleep(max(1, every))
