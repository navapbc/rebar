#!/usr/bin/env python3
"""Detect tickets that become newly unblocked when a set of tickets is closed.

Usage (CLI):
    python3 ticket-unblock.py <tracker_dir> <ticket_id> [--event-source local-close|sync-resolution]
    python3 ticket-unblock.py --batch-close <tracker_dir> <ticket_id>

Module interface:
    detect_newly_unblocked(
        closed_ticket_ids: list[str],
        tracker_dir: str,
        event_source: str,
        *,
        ticket_states: dict | None = None,
    ) -> list[str]

    batch_close_operations(
        ticket_ids: list[str],
        tracker_dir: str,
        exclude_archived: bool = True,
    ) -> dict
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load ticket-reducer.py via importlib (hyphenated filename)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REDUCER_PATH = _SCRIPTS_DIR / "ticket-reducer.py"


def _load_reducer():
    """Load the ticket-reducer module via importlib (hyphenated filename)."""
    spec = importlib.util.spec_from_file_location("ticket_reducer", _REDUCER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load ticket-reducer.py from {_REDUCER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_reducer_module = None


def _get_reducer():
    global _reducer_module
    if _reducer_module is None:
        _reducer_module = _load_reducer()
    return _reducer_module


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

_BLOCKING_RELATIONS = {"blocks", "depends_on"}
_CLOSED_STATUSES = {"closed", "deleted"}
_VALID_EVENT_SOURCES = {"local-close", "sync-resolution"}


def _is_closed(status: str) -> bool:
    """Return True if a ticket status is considered closed/done."""
    return status in _CLOSED_STATUSES


def detect_newly_unblocked(
    closed_ticket_ids: list[str],
    tracker_dir: str,
    event_source: str,
    *,
    ticket_states: dict | None = None,
) -> list[str]:
    """Return ticket IDs that become ready_to_work after closing closed_ticket_ids.

    A ticket is newly unblocked when:
      - Its status is open (not already closed), AND
      - All of its direct blockers (blocks/depends_on relations) are now closed,
        counting closed_ticket_ids as closed regardless of their current state.

    Performs a single batch graph traversal — not one query per closed ticket.

    Args:
        closed_ticket_ids: Ticket IDs that are being closed in this operation.
        tracker_dir: Path to the tickets tracker directory.
        event_source: Either 'local-close' or 'sync-resolution'.
        ticket_states: Optional pre-loaded ticket states dict (keyed by ticket_id).
            When provided, skips the internal os.scandir scan. When None (default),
            performs its own scan for backward compatibility.

    Returns:
        List of ticket IDs (strings) that are newly unblocked. Empty list if none.

    Raises:
        ValueError: If event_source is not a valid value.
    """
    if event_source not in _VALID_EVENT_SOURCES:
        raise ValueError(
            f"Invalid event_source {event_source!r}. "
            f"Must be one of: {sorted(_VALID_EVENT_SOURCES)}"
        )

    tracker_path = Path(tracker_dir)

    # Treat closed_ticket_ids as a set for O(1) lookup.
    newly_closed_set = set(closed_ticket_ids)

    # --------------------------------------------------------------------------
    # Single-pass: load all ticket states at once (batch graph traversal).
    # When ticket_states is provided, skip the internal scan.
    # --------------------------------------------------------------------------
    if ticket_states is None:
        reducer = _get_reducer()
        reduce_ticket = reducer.reduce_ticket

        if not tracker_path.is_dir():
            return []

        ticket_states = {}
        for entry in os.scandir(tracker_path):
            if not entry.is_dir():
                continue
            # Skip hidden directories (.suggestions, .review-events, .index, etc.)
            # — they are not ticket dirs and their JSON files are not ticket events.
            if entry.name.startswith("."):
                continue
            ticket_id = entry.name
            # Tombstone-aware: .tombstone.json written by ticket delete carries the
            # terminal status; the reducer does not read it.
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
            # Skip error/fsck states — treat as non-existent
            if state.get("status") in ("error", "fsck_needed"):
                continue
            # Override status from tombstone (reducer does not read tombstone)
            if tombstone_status is not None:
                state["status"] = tombstone_status
            ticket_states[ticket_id] = state
    # else: ticket_states was provided — use it directly (empty dict is valid:
    # it means the caller scanned and found no tickets).

    def ticket_is_closed(ticket_id: str) -> bool:
        """Return True if ticket_id is closed, either actually or in the batch."""
        if ticket_id in newly_closed_set:
            return True
        state = ticket_states.get(ticket_id)
        if state is None:
            # Missing ticket dir → tombstoned, treat as closed
            return True
        return _is_closed(state.get("status", "open"))

    # --------------------------------------------------------------------------
    # Find tickets newly unblocked by the batch close.
    # --------------------------------------------------------------------------
    # Build a reverse map: blocker_id → list of ticket_ids it blocks.
    # deps list in compiled state contains: {target_id, relation, link_uuid}
    # target_id is the ticket being blocked (the LINK event is in the blocker's dir).
    # So state["deps"] for ticket X lists: "X blocks target_id".
    # We need: for each candidate ticket C, find all tickets that block C.
    #
    # Strategy: for each ticket, iterate its deps to find what it blocks,
    # then for each candidate check if all its blockers are now closed.

    # blocked_by[ticket_id] = set of ticket_ids that block it (direct blockers)
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
                # LINK event is in ticket_id's dir: ticket_id blocks target_id
                blocker_id = ticket_id
                blocked_id = target_id
            else:
                # relation == "depends_on"
                # LINK event is in ticket_id's dir: ticket_id depends_on target_id
                # → target_id is the blocker; ticket_id is the blocked ticket
                blocker_id = target_id
                blocked_id = ticket_id
            if blocked_id not in blocked_by:
                blocked_by[blocked_id] = set()
            blocked_by[blocked_id].add(blocker_id)

    newly_unblocked: list[str] = []

    for ticket_id, state in ticket_states.items():
        # Only consider open tickets (not already closed)
        if _is_closed(state.get("status", "open")):
            continue
        # Skip tickets in the batch being closed
        if ticket_id in newly_closed_set:
            continue

        blockers = blocked_by.get(ticket_id, set())
        if not blockers:
            # No blockers — already unblocked (not "newly" unblocked)
            continue

        # Was this ticket already unblocked BEFORE the batch close?
        # i.e., were all blockers already closed before this operation?
        all_blockers_were_closed_before = all(
            _is_closed(ticket_states.get(b, {}).get("status", "open"))
            if b not in newly_closed_set
            else False  # blocker was open before (it's in newly_closed_set)
            for b in blockers
        )

        if all_blockers_were_closed_before:
            # Already unblocked before the batch — not "newly" unblocked
            continue

        # Are all blockers closed NOW (after the batch)?
        all_blockers_closed_now = all(ticket_is_closed(b) for b in blockers)

        if all_blockers_closed_now:
            newly_unblocked.append(ticket_id)

    return newly_unblocked


# ---------------------------------------------------------------------------
# Batch close operations
# ---------------------------------------------------------------------------


def batch_close_operations(
    ticket_ids: list[str],
    tracker_dir: str,
    exclude_archived: bool = True,
) -> dict:
    """Compute open children and newly unblocked tickets for a batch close.

    Calls reduce_all_tickets once, builds a ticket_states dict, then:
    - Finds open children: tickets whose parent_id is in ticket_ids and whose
      status is not closed.
    - Finds newly unblocked: tickets that become unblocked after closing ticket_ids.

    Args:
        ticket_ids: Ticket IDs being closed in this operation.
        tracker_dir: Path to the tickets tracker directory.
        exclude_archived: Whether to exclude archived tickets (default: True).

    Returns:
        dict with keys:
            "open_children": list of ticket IDs that are open children of ticket_ids
            "newly_unblocked": list of ticket IDs newly unblocked by closing ticket_ids
    """
    tracker_path = Path(tracker_dir)
    if not tracker_path.is_dir():
        return {"open_children": [], "newly_unblocked": []}

    reducer = _get_reducer()
    all_states = reducer.reduce_all_tickets(
        tracker_dir, exclude_archived=exclude_archived
    )

    # Build ticket_states dict keyed by ticket_id, filtering out error states.
    ts: dict[str, dict] = {}
    for state in all_states:
        tid = state.get("ticket_id")
        if not tid:
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        ts[tid] = state

    # Open children check: tickets whose parent_id is in ticket_ids and not closed.
    ticket_ids_set = set(ticket_ids)
    open_children: list[str] = [
        tid
        for tid, state in ts.items()
        if state.get("parent_id") in ticket_ids_set
        and not _is_closed(state.get("status", "open"))
    ]

    # Unblock detection: reuse detect_newly_unblocked with pre-loaded state.
    newly_unblocked = detect_newly_unblocked(
        closed_ticket_ids=ticket_ids,
        tracker_dir=tracker_dir,
        event_source="local-close",
        ticket_states=ts,
    )

    return {"open_children": open_children, "newly_unblocked": newly_unblocked}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI: ticket-unblock.py <tracker_dir> <ticket_id> [--event-source ...]
    ticket-unblock.py --batch-close <tracker_dir> <ticket_id>
    """
    import argparse
    import json as _json

    # Handle --batch-close mode before argparse to keep positional args simple.
    if len(sys.argv) >= 2 and sys.argv[1] == "--batch-close":
        if len(sys.argv) < 4:
            print(
                "Usage: ticket-unblock.py --batch-close <tracker_dir> <ticket_id>",
                file=sys.stderr,
            )
            return 1
        tracker_dir = sys.argv[2]
        raw_ticket_id = sys.argv[3]
        _scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from ticket_resolver import resolve_ticket_id

        ticket_id = resolve_ticket_id(raw_ticket_id, tracker_dir)
        if ticket_id is None:
            print(f"Error: ticket '{raw_ticket_id}' does not exist", file=sys.stderr)
            return 1
        result = batch_close_operations(
            ticket_ids=[ticket_id],
            tracker_dir=tracker_dir,
        )
        print(_json.dumps(result))
        return 0

    parser = argparse.ArgumentParser(
        description="Detect tickets newly unblocked when a ticket is closed.",
    )
    parser.add_argument("tracker_dir", help="Path to the tickets tracker directory.")
    parser.add_argument("ticket_id", help="The ticket ID being closed.")
    parser.add_argument(
        "--event-source",
        default="local-close",
        choices=list(_VALID_EVENT_SOURCES),
        help="Source of the close event (default: local-close).",
    )

    args = parser.parse_args()

    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from ticket_resolver import resolve_ticket_id

    resolved = resolve_ticket_id(args.ticket_id, args.tracker_dir)
    if resolved is None:
        print(f"Error: ticket '{args.ticket_id}' does not exist", file=sys.stderr)
        return 1

    try:
        unblocked = detect_newly_unblocked(
            closed_ticket_ids=[resolved],
            tracker_dir=args.tracker_dir,
            event_source=args.event_source,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for ticket_id in unblocked:
        print(f"UNBLOCKED {ticket_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
