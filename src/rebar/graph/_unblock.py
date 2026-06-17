"""Detect tickets newly unblocked when a set of tickets is closed (Tier E).

Faithful in-package port of ``_engine/ticket-unblock.py`` (the bash-era helper
that ``ticket-transition.sh`` subprocessed for ``--batch-close``). Uses
``rebar.reducer`` directly instead of the importlib-loaded engine reducer. The
ordering contract is load-bearing for byte-parity: ``reduce_all_tickets`` iterates
``sorted(os.listdir(...))`` so ``newly_unblocked`` (hence the comma-joined
``UNBLOCKED: a,b,c`` line and the JSON array) is deterministic.

A ticket is *newly* unblocked when it is open, has at least one blocker, was NOT
already unblocked before the batch close, and all its direct blockers
(``blocks``/``depends_on``) are closed once the batch is counted as closed.
``.tombstone.json`` carries the terminal status the reducer does not read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from rebar.reducer import reduce_all_tickets, reduce_ticket

_BLOCKING_RELATIONS = {"blocks", "depends_on"}
_CLOSED_STATUSES = {"closed", "deleted"}
_VALID_EVENT_SOURCES = {"local-close", "sync-resolution"}


def _is_closed(status: str) -> bool:
    return status in _CLOSED_STATUSES


def detect_newly_unblocked(
    closed_ticket_ids: list[str],
    tracker_dir: str,
    event_source: str,
    *,
    ticket_states: dict | None = None,
) -> list[str]:
    """Return ticket IDs that become ready_to_work after closing
    ``closed_ticket_ids`` (single batch graph traversal, not one query per close).

    Raises ``ValueError`` if ``event_source`` is not ``local-close`` /
    ``sync-resolution``."""
    if event_source not in _VALID_EVENT_SOURCES:
        raise ValueError(
            f"Invalid event_source {event_source!r}. Must be one of: {sorted(_VALID_EVENT_SOURCES)}"
        )

    tracker_path = Path(tracker_dir)
    newly_closed_set = set(closed_ticket_ids)

    if ticket_states is None:
        if not tracker_path.is_dir():
            return []
        ticket_states = {}
        for entry in os.scandir(tracker_path):
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            ticket_id = entry.name
            tombstone_path = Path(entry.path) / ".tombstone.json"
            tombstone_status: str | None = None
            if tombstone_path.is_file():
                try:
                    _ts = json.loads(tombstone_path.read_text())
                    tombstone_status = str(_ts.get("status", "deleted"))
                except Exception:
                    tombstone_status = "deleted"
            state = reduce_ticket(entry.path)
            if state is None:
                if tombstone_status is not None:
                    ticket_states[ticket_id] = {"status": tombstone_status}
                continue
            if state.get("status") in ("error", "fsck_needed"):
                continue
            if tombstone_status is not None:
                state["status"] = tombstone_status
            ticket_states[ticket_id] = state

    def ticket_is_closed(ticket_id: str) -> bool:
        if ticket_id in newly_closed_set:
            return True
        state = ticket_states.get(ticket_id)
        if state is None:
            return True  # missing dir → tombstoned, treat as closed
        return _is_closed(state.get("status", "open"))

    # blocked_by[ticket_id] = set of ticket_ids that block it (direct blockers).
    blocked_by: dict[str, set[str]] = {}
    for ticket_id, state in ticket_states.items():
        for dep in state.get("deps", []):
            relation = dep.get("relation")
            if relation not in _BLOCKING_RELATIONS:
                continue
            target_id = dep.get("target_id")
            if not target_id:
                continue
            if relation == "blocks":
                blocker_id, blocked_id = ticket_id, target_id
            else:  # depends_on: target_id blocks ticket_id
                blocker_id, blocked_id = target_id, ticket_id
            blocked_by.setdefault(blocked_id, set()).add(blocker_id)

    newly_unblocked: list[str] = []
    for ticket_id, state in ticket_states.items():
        if _is_closed(state.get("status", "open")):
            continue
        if ticket_id in newly_closed_set:
            continue
        blockers = blocked_by.get(ticket_id, set())
        if not blockers:
            continue  # no blockers — already unblocked, not "newly"
        all_blockers_were_closed_before = all(
            _is_closed(ticket_states.get(b, {}).get("status", "open"))
            if b not in newly_closed_set
            else False
            for b in blockers
        )
        if all_blockers_were_closed_before:
            continue
        if all(ticket_is_closed(b) for b in blockers):
            newly_unblocked.append(ticket_id)
    return newly_unblocked


def batch_close_operations(
    ticket_ids: list[str],
    tracker_dir: str,
    exclude_archived: bool = True,
) -> dict:
    """Compute ``open_children`` (tickets parented to ``ticket_ids`` that are not
    closed) and ``newly_unblocked`` for a batch close, in one ``reduce_all_tickets``
    pass."""
    tracker_path = Path(tracker_dir)
    if not tracker_path.is_dir():
        return {"open_children": [], "newly_unblocked": []}

    # Exclude session_logs: they never block/unblock anything, and a lifecycle-exempt
    # session_log child must never count as an "open child" that blocks a parent close.
    all_states = reduce_all_tickets(
        tracker_dir, exclude_archived=exclude_archived, exclude_session_logs=True
    )

    ts: dict[str, dict] = {}
    for state in all_states:
        tid = state.get("ticket_id")
        if not tid:
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        ts[tid] = state

    ticket_ids_set = set(ticket_ids)
    open_children: list[str] = [
        tid
        for tid, state in ts.items()
        if state.get("parent_id") in ticket_ids_set and not _is_closed(state.get("status", "open"))
    ]

    newly_unblocked = detect_newly_unblocked(
        closed_ticket_ids=ticket_ids,
        tracker_dir=tracker_dir,
        event_source="local-close",
        ticket_states=ts,
    )
    return {"open_children": open_children, "newly_unblocked": newly_unblocked}
