"""Event-sourced enrichment queue (epic only-crave-art, story e1f4).

Enrichment must run async, off the hot write path. rebar's event store is already a durable,
shared, serialized-write queue substrate — better than a bolted-on SQLite/broker (which
could not live on the shared tickets path nor be visible to all clones). Three sidecar
events keyed by ticket_id model the queue:

  * ``ENQUEUE_ENRICH {ticket_id, not_before_ns}`` — appended on plan-review CERTIFICATION,
    ``not_before_ns = now + SOAK`` (default 60 min). A re-certification appends a fresh one
    that SUPERSEDES the prior (latest-wins by event timestamp) and bumps ``not_before_ns``
    forward — a debounce/soak enforced by a STORED timestamp, filtered at drain time (NOT an
    in-process timer, which would starve under continuous edits and not survive short-lived
    processes).
  * ``CLAIM_ENRICH {ticket_id, drainer_id, lease_expires_ns}`` — an OPTIMISTIC append; among
    the claims after the latest enqueue, the earliest ``(timestamp, uuid)`` wins (exactly one
    drainer lands, losers re-read and pick another). A lease bounds crashes → at-least-once →
    the summarization is idempotent (overwrite-by-content-hash in the digest sidecar).
  * ``DONE_ENRICH {ticket_id}`` — appended by the drainer after a successful enrich.

The events are reducer-IGNORED (like REVIEW_RESULT / TICKET_DIGEST) — they never enter
compiled ticket state; ``pending_enrichment`` reduces them out-of-band at drain time.

**Why optimistic-append, not a held lock:** ``append_event`` self-serializes every write
under the store write lock, so a claim CANNOT hold ``lock.acquire`` across its own append
(the non-reentrant mkdir leg would deadlock). The epic's design is therefore optimistic:
append, then arbitrate by the earliest post-enqueue claim.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENQUEUE = "ENQUEUE_ENRICH"
CLAIM = "CLAIM_ENRICH"
DONE = "DONE_ENRICH"
QUEUE_EVENT_TYPES = (ENQUEUE, CLAIM, DONE)

_NS_PER_MIN = 60 * 1_000_000_000


def _now_ns() -> int:
    from rebar._store import hlc

    return hlc.physical_now()


def _tracker(repo_root) -> str:
    from rebar import config as _config

    return str(_config.tracker_dir(repo_root))


def _resolve(rid: str, tracker: str) -> str:
    from rebar._engine_support.resolver import resolve_ticket_id

    return resolve_ticket_id(rid, tracker) or rid


def _append(ticket_id: str, event_type: str, data: dict, repo_root) -> bool:
    from rebar._commands._seam import append_event

    try:
        append_event(ticket_id, event_type, data, Path(_tracker(repo_root)), repo_root=repo_root)
        return True
    except Exception:  # noqa: BLE001 — best-effort queue write; broad-but-logged, never fails caller
        logger.warning("%s append failed; continuing", event_type, exc_info=True)
        return False


def enqueue(ticket_id: str, *, soak_min: float, repo_root=None, now_ns: int | None = None) -> bool:
    """Append ``ENQUEUE_ENRICH`` with ``not_before_ns = now + soak_min``. A re-enqueue
    supersedes (latest-wins), bumping ``not_before_ns`` forward. Best-effort."""
    now = now_ns if now_ns is not None else _now_ns()
    not_before = now + int(soak_min * _NS_PER_MIN)
    return _append(
        ticket_id, ENQUEUE, {"ticket_id": ticket_id, "not_before_ns": not_before}, repo_root
    )


def mark_done(ticket_id: str, *, repo_root=None) -> bool:
    """Append ``DONE_ENRICH`` (the drainer's completion tombstone). Best-effort."""
    return _append(ticket_id, DONE, {"ticket_id": ticket_id}, repo_root)


def _events_of(ticket_dir: str, event_type: str) -> list[tuple[int, str, dict]]:
    """All ``event_type`` events in ``ticket_dir`` as ``(timestamp, uuid, data)``, oldest
    first (by the filename timestamp prefix). Tolerates unreadable files."""
    out: list[tuple[int, str, dict]] = []
    try:
        names = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{event_type}.json") and not f.startswith(".")
        )
    except OSError:
        return out
    for fname in names:
        try:
            with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError):
            continue
        ts = event.get("timestamp")
        data = event.get("data")
        if isinstance(ts, int) and isinstance(data, dict):
            out.append((ts, str(event.get("uuid", "")), data))
    return out


def _latest(ticket_dir: str, event_type: str) -> tuple[int, str, dict] | None:
    evs = _events_of(ticket_dir, event_type)
    return evs[-1] if evs else None


def reduce_ticket(ticket_id: str, tracker: str, *, now_ns: int | None = None) -> dict[str, Any]:
    """The queue state for one ticket: ``{enqueued, not_before_ns, claimed, lease_expires_ns,
    done, pending}`` derived from its latest ENQUEUE/CLAIM/DONE events."""
    now = now_ns if now_ns is not None else _now_ns()
    ticket_dir = os.path.join(tracker, _resolve(ticket_id, tracker))
    enq = _latest(ticket_dir, ENQUEUE)
    done = _latest(ticket_dir, DONE)
    claim = _latest(ticket_dir, CLAIM)
    state: dict[str, Any] = {
        "enqueued": enq is not None,
        "not_before_ns": enq[2].get("not_before_ns") if enq else None,
        "claimed": False,
        "lease_expires_ns": None,
        "done": False,
        "pending": False,
    }
    if enq is None:
        return state
    enq_ts = enq[0]
    # DONE after the latest enqueue → nothing pending.
    if done is not None and done[0] > enq_ts:
        state["done"] = True
        return state
    # A claim after the latest enqueue with a live lease → claimed (not pending).
    if claim is not None and claim[0] > enq_ts:
        lease = claim[2].get("lease_expires_ns", 0)
        state["lease_expires_ns"] = lease
        if now < lease:
            state["claimed"] = True
            return state
    # Past the soak, unclaimed-or-expired, not done → pending.
    if now >= (state["not_before_ns"] or 0):
        state["pending"] = True
    return state


def pending_enrichment(now_ns: int, tracker: str) -> list[str]:
    """All ticket ids past soak, unclaimed-or-lease-expired, with no later DONE."""
    out: list[str] = []
    try:
        entries = os.listdir(tracker)
    except OSError:
        return out
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker, name)):
            continue
        if reduce_ticket(name, tracker, now_ns=now_ns)["pending"]:
            out.append(name)
    return sorted(out)


def claim(
    ticket_id: str,
    drainer_id: str,
    *,
    lease_ttl_min: float,
    now_ns: int | None = None,
    repo_root=None,
) -> bool:
    """Optimistically claim ``ticket_id`` for ``drainer_id``. Returns True iff THIS drainer
    won (its claim is the earliest ``(timestamp, uuid)`` after the latest enqueue). Losers
    return False and re-read to pick another. A lease-expired prior claim is superseded."""
    tracker = _tracker(repo_root)
    now = now_ns if now_ns is not None else _now_ns()
    if not reduce_ticket(ticket_id, tracker, now_ns=now)["pending"]:
        return False
    lease = now + int(lease_ttl_min * _NS_PER_MIN)
    if not _append(
        ticket_id,
        CLAIM,
        {"ticket_id": ticket_id, "drainer_id": drainer_id, "lease_expires_ns": lease},
        repo_root,
    ):
        return False
    # Arbitrate: among CLAIM events after the latest enqueue whose lease is STILL LIVE at
    # `now`, the earliest (ts, uuid) wins. Filtering by live lease is what makes lease expiry
    # self-healing — an expired prior claim is not a contender, so the next claimant wins.
    ticket_dir = os.path.join(tracker, _resolve(ticket_id, tracker))
    enq = _latest(ticket_dir, ENQUEUE)
    if enq is None:
        return False
    contenders = [
        (ts, uuid, data)
        for (ts, uuid, data) in _events_of(ticket_dir, CLAIM)
        if ts > enq[0] and now < data.get("lease_expires_ns", 0)
    ]
    if not contenders:
        return False
    winner = min(contenders, key=lambda e: (e[0], e[1]))
    return winner[2].get("drainer_id") == drainer_id
