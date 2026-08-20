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
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENQUEUE = "ENQUEUE_ENRICH"
CLAIM = "CLAIM_ENRICH"
DONE = "DONE_ENRICH"
QUEUE_EVENT_TYPES = (ENQUEUE, CLAIM, DONE)

_NS_PER_MIN = 60 * 1_000_000_000

_GATE_MARKER_NAME = "enrich-gate.json"

# How long a gate marker may be trusted before the full scan is forced again.
#
# The marker is a CACHE, and a cache on a shared store needs an expiry that bounds every way
# it can go wrong. Ten minutes is chosen to sit well inside the default 60-minute enrichment
# soak: a marker can therefore never span a whole soak window, so the worst case for a marker
# that went stale without being invalidated is a drain deferred by <= 10 minutes on a feature
# whose latency budget is already measured in tens of minutes — never a drain that is LOST.
# That is exactly the crash window the TTL exists for: if a process dies between appending a
# queue event and clearing the marker (step 3 below), the stale marker survives, and only the
# TTL retires it. Bounded delay, never permanent loss.
_GATE_MARKER_TTL_NS = 10 * _NS_PER_MIN


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
        tracker = _tracker(repo_root)
        append_event(ticket_id, event_type, data, Path(tracker), repo_root=repo_root)
    except Exception:
        logger.warning("%s append failed; continuing", event_type, exc_info=True)
        return False
    # ENQUEUE / CLAIM / DONE all funnel through here, so this one call is the complete
    # invalidation of the gate marker's "nothing can be pending yet" claim: any queue state
    # change drops the marker and the next probe re-scans (bug moist-short-lionfish).
    _clear_gate_marker(tracker)
    return True


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


def _event_names(ticket_dir: str, event_type: str) -> list[str]:
    """Matching event filenames, oldest first. Names are ``{timestamp}-{uuid}-{TYPE}.json``
    (``_store/event_append.event_filename``, built from the event's OWN validated
    ``timestamp``), and the timestamps are fixed-width ns integers — so lexicographic name
    order IS timestamp order. Tolerates a missing/unreadable directory."""
    try:
        return sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{event_type}.json") and not f.startswith(".")
        )
    except OSError:
        return []


def _read_event(ticket_dir: str, fname: str) -> tuple[int, str, dict] | None:
    """One event as ``(timestamp, uuid, data)``, or ``None`` if unreadable/malformed."""
    try:
        with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    ts, data = event.get("timestamp"), event.get("data")
    if isinstance(ts, int) and isinstance(data, dict):
        return (ts, str(event.get("uuid", "")), data)
    return None


def _name_ts(fname: str) -> int | None:
    """The filename's leading timestamp, or ``None`` if it does not carry one."""
    prefix = fname.split("-", 1)[0]
    return int(prefix) if prefix.isdigit() else None


def _latest(ticket_dir: str, event_type: str) -> tuple[int, str, dict] | None:
    """The newest usable ``event_type`` event, walking newest→oldest and returning the FIRST
    parseable one — so an intact newest file costs exactly ONE open regardless of how much
    history the ticket carries (the shape ``digest_sidecar.latest_ticket_digest`` already
    uses). If the walk exhausts every matching name (all corrupt, or none present) the type
    is absent and ``None`` is returned, exactly as before."""
    for fname in reversed(_event_names(ticket_dir, event_type)):
        event = _read_event(ticket_dir, fname)
        if event is not None:
            return event
        logger.warning("%s event %s unreadable; trying older", event_type, fname)
    return None


def _claims_after(ticket_dir: str, enq_ts: int) -> list[tuple[int, str, dict]]:
    """CLAIM events strictly newer than ``enq_ts``. Those are a contiguous SUFFIX of the
    name-sorted list, so walk newest→oldest and stop at the first name whose timestamp
    prefix is ``<= enq_ts`` — the read is bounded by the contention window, not by history.
    A name with no parseable timestamp prefix is skipped rather than treated as a stop, and
    the body's own ``timestamp`` still decides membership."""
    out: list[tuple[int, str, dict]] = []
    for fname in reversed(_event_names(ticket_dir, CLAIM)):
        name_ts = _name_ts(fname)
        if name_ts is not None and name_ts <= enq_ts:
            break
        event = _read_event(ticket_dir, fname)
        if event is not None and event[0] > enq_ts:
            out.append(event)
    return out


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


# --- O(1) gate fast path (bug 958e-f4b3-275c-4154 / moist-short-lionfish) -------------------
#
# ``enrich_drain.maybe_drain`` probes ``pending_enrichment`` on EVERY ticket store write, and
# the honest answer is "nothing is soaked" almost every time. Paying for that answer with a
# full store walk — one ``reduce_ticket`` per ticket directory, each doing three ``listdir``s
# plus two ``isdir`` stats — measured ~24k filesystem metadata syscalls and 486 ms against a
# 20 ms gate budget on 100% of writes, and 2.8-3.6 s under concurrent agents contending on
# kernel vnode state.
#
# The fix is to remember the ANSWER rather than recompute it. The queue is a soak queue, so a
# quiet store is not merely quiet now — it is provably quiet until the earliest instant any
# live entry could become pending. One scan can compute that instant, and every probe before
# it is then answerable in O(1) with a single small file read.
#
# The marker is machine-local cache state, so it lives in the repo-local ``.rebar`` directory
# (the tracker's SIBLING, the same place ``enrich_drain._drain_lock_path`` uses) and NEVER
# inside the tracker — the tracker is a git-backed shared store that auto-commits and
# auto-pushes, and a per-host cache file has no business travelling to every clone.
#
# Three rules keep it honest:
#   1. It is only ever trusted to say "nothing is pending" (a fast ``[]``). It can never
#      manufacture a pending ticket, so a wrong marker costs a delayed drain, never a wrong
#      one.
#   2. Every queue append clears it (see ``_append``), so a state change is never hidden.
#   3. It expires (``_GATE_MARKER_TTL_NS``), so even a marker that outlives its invalidation
#      is retired on a clock rather than trusted forever.
#
# ACCEPTED RACE, and rule 3 is its bound. Two windows let a marker outlive its invalidation:
# a process dying between appending a queue event and clearing the marker, and an append that
# lands DURING a scan, whose clear is then overwritten by the marker that same scan writes
# afterwards. Neither is excluded here, deliberately:
#   * The harm is a DEFERRED drain, never a wrong or lost one — the marker can only ever say
#     "nothing is pending", so the worst case is that a soaked ticket waits out the TTL.
#   * That wait is <= 10 minutes on a feature whose soak is 60 minutes by default, i.e. inside
#     the latency this queue already accepts by design.
#   * Closing it means adding cross-process serialization (a generation token, or taking the
#     store lock across the scan) to a path whose entire purpose is to be cheaper than a
#     store walk — more machinery, and more contention, than the delay it would prevent.
# Revisit if the soak ever drops near the TTL, which would stop the bound being comfortable.
# Every read and write is best-effort: a missing, empty, truncated, non-JSON, wrong-typed or
# otherwise unusable marker degrades to the full scan, which is the pre-existing behaviour.


def _gate_marker_path(tracker: str) -> str:
    """The gate marker file, in the repo-local ``.rebar`` dir beside *tracker*."""
    return os.path.join(os.path.dirname(tracker), ".rebar", _GATE_MARKER_NAME)


def _clear_gate_marker(tracker: str) -> None:
    """Drop the marker so the next probe re-scans. Best-effort and silent: this runs on the
    store write path, where a cache-maintenance failure must never surface as an error."""
    try:
        os.unlink(_gate_marker_path(tracker))
    except OSError:
        pass
    except Exception:  # pragma: no cover — path resolution on an exotic tracker value
        logger.debug("enrich gate: could not clear marker", exc_info=True)


def _read_gate_marker(tracker: str) -> tuple[int | None, int] | None:
    """``(next_eligible_ns, written_ns)`` from the marker, or ``None`` if it cannot be
    trusted. Anything unexpected — absent, unreadable, not JSON, not an object, missing or
    wrong-typed fields — is reported as untrusted rather than raised, because the caller's
    fallback (a full scan) is always correct and an exception here is not."""
    try:
        with open(_gate_marker_path(tracker), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        logger.debug("enrich gate: marker unreadable; falling back to a full scan", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    written = payload.get("written_ns")
    nxt = payload.get("next_eligible_ns")
    if not isinstance(written, int) or isinstance(written, bool):
        return None
    if nxt is not None and (not isinstance(nxt, int) or isinstance(nxt, bool)):
        return None
    return (nxt, written)


def _gate_marker_says_quiet(now_ns: int, tracker: str) -> bool:
    """True iff a live marker proves nothing can be pending at *now_ns*. ``next_eligible_ns``
    is ``None`` when the last scan found no live entry at all — then only the TTL limits the
    fast path."""
    marker = _read_gate_marker(tracker)
    if marker is None:
        return False
    next_eligible, written = marker
    # A marker stamped in the FUTURE is untrusted, not infinitely fresh. The TTL is a
    # subtraction, so a clock that stepped backwards (or a marker from a host whose clock ran
    # fast) makes this negative and the TTL could never elapse — turning the bounded-delay
    # guarantee into an unbounded one. Out-of-range in EITHER direction means rescan.
    if not 0 <= now_ns - written < _GATE_MARKER_TTL_NS:
        return False
    return next_eligible is None or now_ns < next_eligible


def _write_gate_marker(tracker: str, now_ns: int, next_eligible_ns: int | None) -> None:
    """Record "nothing can be pending before *next_eligible_ns*" as of *now_ns*.

    Written via a temp file in the same directory plus ``os.replace`` so a concurrent reader
    sees either the old marker or the new one, never a torn half-file. Best-effort throughout:
    failing to write the marker only forfeits the fast path, and must not fail the probe."""
    path = _gate_marker_path(tracker)
    payload = {"next_eligible_ns": next_eligible_ns, "written_ns": now_ns}
    tmp: str | None = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".enrich-gate-", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        tmp = None
    except Exception:
        logger.debug("enrich gate: could not write marker %s", path, exc_info=True)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _entry_eligible_ns(state: dict[str, Any]) -> int | None:
    """The earliest instant this ticket's queue entry could become pending, or ``None`` if it
    is not live at all. A never-enqueued or already-DONE entry contributes nothing; a claim
    with a still-live lease defers eligibility to the lease expiry; anything else is gated
    only by its soak deadline."""
    if not state.get("enqueued") or state.get("done"):
        return None
    not_before = int(state.get("not_before_ns") or 0)
    if state.get("claimed"):
        return max(not_before, int(state.get("lease_expires_ns") or 0))
    return not_before


def pending_enrichment(now_ns: int, tracker: str) -> list[str]:
    """All ticket ids past soak, unclaimed-or-lease-expired, with no later DONE.

    Answers from the O(1) gate marker when one proves the store is quiet (see the block
    comment above); otherwise falls back to the full scan and, in that SAME pass, records the
    marker for subsequent probes."""
    if _gate_marker_says_quiet(now_ns, tracker):
        return []
    out: list[str] = []
    soonest: int | None = None
    try:
        entries = os.listdir(tracker)
    except OSError:
        return out
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker, name)):
            continue
        state = reduce_ticket(name, tracker, now_ns=now_ns)
        if state["pending"]:
            out.append(name)
        eligible = _entry_eligible_ns(state)
        if eligible is not None and (soonest is None or eligible < soonest):
            soonest = eligible
    if out:
        _clear_gate_marker(tracker)
    else:
        _write_gate_marker(tracker, now_ns, soonest)
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
        for (ts, uuid, data) in _claims_after(ticket_dir, enq[0])
        if now < data.get("lease_expires_ns", 0)
    ]
    if not contenders:
        return False
    winner = min(contenders, key=lambda e: (e[0], e[1]))
    return winner[2].get("drainer_id") == drainer_id
