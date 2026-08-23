"""Create-time advisory same-title duplicate probe (ticket eac3-ed70-764a-4f9e).

Duplicate tickets were only detected after an agent was mid-implementation: the heavy
overlap detector (ADR 0086) deliberately runs at review time, off the hot write path, so a
same-title twin filed seconds after its original passed creation unflagged — and the store
measurement on that ticket showed every same-title true duplicate was created within 171
seconds of its twin ("same normalized title + short window" scored 100% precision).

This module is the cheap create-time complement: a small rolling "recent creates" journal —
a store-wide sidecar beside the canonical tracker (the keying the overlap enrich gate marker
uses, bug ``da68-fc7c``) — records each create and is pruned to a fixed recency window. On
every create the journal is probed for a normalized-title match BEFORE the new ticket's own
entry is appended. Cost is one flock + one small-file read + one write — O(window), never
O(store): no title index and no store scan, honouring the write-path budget lesson of
``moist-short-lionfish`` (486 ms/write against a 20 ms budget).

Concurrency: the whole read-prune-probe-append-rewrite is ONE critical section under a
dedicated exclusive ``fcntl.flock`` (:func:`_journal_lock` — the ``_hlc_lock`` pattern from
:mod:`rebar._store.hlc`), held only for the small-file RMW and never nested with the store
write lock, so two near-simultaneous creates serialize: the second's probe observes the
first's entry (the burst case the feature targets) and no rewrite clobbers a just-appended
row. The kernel drops the lock if its holder dies — no staleness logic.

Advisory ONLY, gated on the existing ``verify.suggest_duplicate_tickets`` key: the flag off
means no journal, no probe, zero write-path footprint; and every failure inside the probe
(unreadable journal, config error, reduce failure) degrades to no-warning, because an
advisory must never fail a completed write.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("rebar")

_JOURNAL_NAME = "recent-creates.json"
_LOCK_NAME = "recent-creates.lock"

#: How long a create stays probe-visible. 600 s is ~3.5x the largest observed true-duplicate
#: delta (171 s) from the eac3 store measurement; the same measurement found same-title pairs
#: outside a short window are legitimate (recurring filings, fixtures), so a wider window
#: would only buy false positives.
RECENT_WINDOW_NS = 600 * 1_000_000_000

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_title(title: str) -> str:
    """Fold case, punctuation, and whitespace so cosmetic variants compare equal."""
    return " ".join(_NON_ALNUM.sub(" ", title.lower()).split())


@contextmanager
def _journal_lock(rebar_dir: str) -> Iterator[None]:
    """A dedicated, local exclusive lock for the journal RMW — held only for the one
    small-file read-modify-write, never across the store write lock (no ordering hazard)."""
    import fcntl

    os.makedirs(rebar_dir, exist_ok=True)
    fd = os.open(os.path.join(rebar_dir, _LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_entries(journal_path: str) -> list[dict[str, Any]]:
    """The journal's well-formed entries, or ``[]`` when anything about it cannot be
    trusted — absent, unreadable, not JSON, wrong shape. The caller's fallback (treat the
    window as empty) is always correct, so nothing here raises."""
    try:
        with open(journal_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:  # noqa: BLE001 — an untrusted journal degrades to an empty window
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("ts_ns"), int)
            and not isinstance(entry.get("ts_ns"), bool)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("title_norm"), str)
        ):
            kept.append(entry)
    return kept


def _candidate_status(tracker: Any, ticket_id: str) -> str:
    """The candidate's LIVE status via a single-ticket reduce (O(1), one dir read);
    ``unknown`` on any failure — the advisory is still worth emitting without it."""
    try:
        from rebar.reducer import reduce_ticket

        status = (reduce_ticket(os.path.join(str(tracker), ticket_id)) or {}).get("status")
        return status if isinstance(status, str) and status else "unknown"
    except Exception:  # noqa: BLE001 — best-effort: the advisory is still worth emitting
        return "unknown"


def _probe_and_record(
    journal_path: str, *, ticket_id: str, alias: str, title: str, now_ns: int
) -> list[dict[str, Any]]:
    """One locked RMW body: prune to the window, collect same-title matches, append this
    create's own entry, rewrite atomically. Returns the matches (possibly empty)."""
    from rebar._store.fsutil import atomic_write

    title_norm = normalize_title(title)
    entries = [
        e
        for e in _read_entries(journal_path)
        if 0 <= now_ns - e["ts_ns"] < RECENT_WINDOW_NS and e["id"] != ticket_id
    ]
    matches = [e for e in entries if title_norm and e["title_norm"] == title_norm]
    entries.append(
        {
            "ts_ns": now_ns,
            "id": ticket_id,
            "alias": alias,
            "title": title,
            "title_norm": title_norm,
        }
    )
    atomic_write(journal_path, json.dumps({"entries": entries}), encoding="utf-8")
    return matches


def duplicate_create_warning(
    tracker: Any, *, ticket_id: str, alias: str | None, title: str, cfg_root: str
) -> str | None:
    """The create-time advisory: a recent same-normalized-title create, or ``None``.

    Called by ``create_core`` AFTER the CREATE event lands (the ticket exists either way).
    Runs only when ``verify.suggest_duplicate_tickets`` is enabled — the same key that turns
    on the review-time overlap detector, so one switch governs "suggest duplicates" on both
    seams. Each surface emits the returned text on its own channel (CLI stderr, library
    logger, MCP result field), exactly as ``description_cap_warning`` does.
    """
    try:
        from rebar.config import compose_config

        if not compose_config(cfg_root).verify.suggest_duplicate_tickets:
            return None

        from rebar._store.paths import StorePaths

        paths = StorePaths(str(tracker))
        now_ns = time.time_ns()
        with _journal_lock(paths.rebar_dir):
            matches = _probe_and_record(
                paths.sidecar(_JOURNAL_NAME),
                ticket_id=ticket_id,
                alias=alias or "",
                title=title,
                now_ns=now_ns,
            )
        if not matches:
            return None
        candidate = max(matches, key=lambda e: e["ts_ns"])
        cand_ref = candidate.get("alias") or candidate["id"]
        status = _candidate_status(tracker, candidate["id"])
        age_s = max(0, (now_ns - candidate["ts_ns"]) // 1_000_000_000)
        new_ref = alias or ticket_id
    except Exception:  # an advisory notice must never fail a completed write
        logger.debug("duplicate-create probe failed for %s", ticket_id, exc_info=True)
        return None
    return (
        f"possible duplicate: '{title}' matches the title of {cand_ref} "
        f"(status {status}, created {age_s}s ago). Advisory only — {new_ref} was still "
        f"created; if it duplicates {cand_ref}, record it with: "
        f"rebar link {new_ref} {cand_ref} duplicates"
    )
