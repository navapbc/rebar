"""Detect tickets newly unblocked when a set of tickets is closed (Tier E).

Faithful in-package port of ``_engine/ticket-unblock.py`` (the bash-era helper
that ``ticket-transition.sh`` subprocessed for ``--batch-close``). Uses
``rebar.reducer`` directly instead of the importlib-loaded engine reducer. The
ordering contract is load-bearing for byte-parity: ``reduce_all_tickets`` iterates
``sorted(os.listdir(...))`` so ``newly_unblocked`` (hence the comma-joined
``unblocked: a,b,c`` confirmation segment and the JSON array) is deterministic.

A ticket is *newly* unblocked when it is open, has at least one blocker, was NOT
already unblocked before the batch close, and all its direct blockers
(``blocks``/``depends_on``) are closed once the batch is counted as closed.
``.tombstone.json`` carries the terminal status the reducer does not read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from rebar.reducer import is_terminal_status, reduce_all_tickets, reduce_ticket

from ._relations import build_blocked_by

_VALID_EVENT_SOURCES = {"local-close", "sync-resolution"}


def _is_closed(status: str) -> bool:
    return is_terminal_status(status)


def _read_tombstone_status(ticket_dir: str) -> str | None:
    """Return the terminal status recorded in ``.tombstone.json``, or None if absent.

    A malformed tombstone still counts as one: the file's presence is the signal,
    so an unreadable payload degrades to ``deleted`` rather than to "no tombstone".
    """
    tombstone_path = Path(ticket_dir) / ".tombstone.json"
    if not tombstone_path.is_file():
        return None
    try:
        return str(json.loads(tombstone_path.read_text()).get("status", "deleted"))
    except Exception:  # noqa: BLE001 — tombstone read best-effort: a malformed/missing tombstone defaults to deleted
        return "deleted"


def _load_states_with_tombstones(tracker_path: Path) -> dict[str, dict]:
    """Load ticket states with the tombstone status overlaid, indexed by ticket id.

    Deliberately NOT ``_loader.load_indexed_states``: the readiness computation needs
    the terminal status of tickets whose events the reducer cannot see, so a
    tombstoned dir yields a status-only stub instead of being dropped, and the
    tombstone status overrides the reduced one. ``error``/``fsck_needed`` states are
    still skipped.
    """
    states: dict[str, dict] = {}
    for entry in os.scandir(tracker_path):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        tombstone_status = _read_tombstone_status(entry.path)
        state = reduce_ticket(entry.path)
        if state is None:
            if tombstone_status is not None:
                states[entry.name] = {"status": tombstone_status}
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        if tombstone_status is not None:
            state["status"] = tombstone_status
        states[entry.name] = state
    return states


def _is_closed_after_batch(ticket_id: str, ticket_states: dict, newly_closed_set: set[str]) -> bool:
    """True when ``ticket_id`` counts as closed once the batch close is applied."""
    if ticket_id in newly_closed_set:
        return True
    state = ticket_states.get(ticket_id)
    if state is None:
        return True  # missing dir → tombstoned, treat as closed
    return _is_closed(state.get("status", "open"))


def _was_already_unblocked(
    blockers: set[str], ticket_states: dict, newly_closed_set: set[str]
) -> bool:
    """True when every blocker was ALREADY closed BEFORE the batch.

    Such a ticket is unblocked but not *newly* so; a blocker in the batch counts as
    open here precisely because the batch has not happened yet.
    """
    return all(
        False
        if blocker in newly_closed_set
        else _is_closed(ticket_states.get(blocker, {}).get("status", "open"))
        for blocker in blockers
    )


def _select_newly_unblocked(
    ticket_states: dict, blocked_by: dict[str, set[str]], newly_closed_set: set[str]
) -> list[str]:
    """Pick the open, not-being-closed tickets that flip to unblocked from this batch.

    Iterates ``ticket_states`` in insertion order, which ``reduce_all_tickets``
    derives from ``sorted(os.listdir(...))`` — the ordering contract the caller's
    byte-parity docstring depends on.
    """
    newly_unblocked: list[str] = []
    for ticket_id, state in ticket_states.items():
        if _is_closed(state.get("status", "open")) or ticket_id in newly_closed_set:
            continue
        blockers = blocked_by.get(ticket_id, set())
        if not blockers:
            continue  # no blockers — already unblocked, not "newly"
        if _was_already_unblocked(blockers, ticket_states, newly_closed_set):
            continue
        if all(_is_closed_after_batch(b, ticket_states, newly_closed_set) for b in blockers):
            newly_unblocked.append(ticket_id)
    return newly_unblocked


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
        ticket_states = _load_states_with_tombstones(tracker_path)

    # blocked_id → set of blocker_ids (shared blocking-edge inversion).
    blocked_by = build_blocked_by(ticket_states)
    return _select_newly_unblocked(ticket_states, blocked_by, newly_closed_set)


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
