"""Ready-to-work computation (single source of truth).

Extracted verbatim from ``ticket-ready.py`` (recommendation-#2 Step 1) so the CLI
script and the in-process library share ONE implementation. A ticket is "ready"
when:
  1. its status is "open" or "in_progress", and
  2. every direct blocker (a dep with relation "depends_on" or "blocks") is
     "closed" (or tombstoned / missing).

Kept dependency-light on purpose: it imports only ``reduce_all_tickets`` from the
``ticket_reducer`` package and does not pull in the heavier ``ticket_graph``
loader/graph modules.
"""

from __future__ import annotations

from pathlib import Path

from rebar.reducer import reduce_all_tickets

from ._relations import _BLOCKING_RELATIONS

_OPEN_STATUSES = {"open", "in_progress"}
_CLOSED_STATUSES = {"closed"}


def _is_closed(status: str) -> bool:
    return status in _CLOSED_STATUSES


def _is_open(status: str) -> bool:
    return status in _OPEN_STATUSES


def find_ready_tickets(
    tracker_dir: str,
    epic_filter: str | None = None,
) -> list[dict]:
    """Return list of ticket state dicts that are ready to work on.

    A ticket is ready when:
    - status is "open" or "in_progress"
    - all direct blockers are closed (or tombstoned / missing)
    - if epic_filter is set, ticket's parent_id must equal epic_filter

    Args:
        tracker_dir: Path to the .tickets-tracker directory.
        epic_filter: Optional epic ID to scope results to direct children.

    Returns:
        List of ticket state dicts (ready tickets only).
    """
    tracker_path = Path(tracker_dir)
    if not tracker_path.is_dir():
        return []

    all_states_list = reduce_all_tickets(str(tracker_dir))

    # Build a lookup dict, skipping error states.
    ticket_states: dict[str, dict] = {}
    for state in all_states_list:
        tid = state.get("ticket_id")
        if not tid:
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        ticket_states[tid] = state

    # Build blocked_by map: blocked_id → set of blocker_ids
    # deps list in a state: each dep has {target_id, relation, link_uuid}
    # LINK event in ticket X's dir means "X relation target_id"
    # - "depends_on": X depends on target_id → target_id blocks X → blocker=target_id, blocked=X
    # - "blocks":     X blocks target_id → blocker=X, blocked=target_id
    blocked_by: dict[str, set[str]] = {}
    for ticket_id, state in ticket_states.items():
        for dep in state.get("deps", []):
            relation = dep.get("relation")
            if relation not in _BLOCKING_RELATIONS:
                continue
            target_id = dep.get("target_id")
            if not target_id:
                continue
            if relation == "depends_on":
                blocker_id = target_id
                blocked_id = ticket_id
            else:  # "blocks"
                blocker_id = ticket_id
                blocked_id = target_id
            blocked_by.setdefault(blocked_id, set()).add(blocker_id)

    def all_blockers_closed(ticket_id: str) -> bool:
        blockers = blocked_by.get(ticket_id, set())
        for blocker_id in blockers:
            blocker_state = ticket_states.get(blocker_id)
            if blocker_state is None:
                # Tombstoned / missing → treat as closed
                continue
            if not _is_closed(blocker_state.get("status", "open")):
                return False
        return True

    ready: list[dict] = []
    for ticket_id, state in ticket_states.items():
        status = state.get("status", "open")
        if not _is_open(status):
            continue
        if epic_filter is not None and state.get("parent_id") != epic_filter:
            continue
        if all_blockers_closed(ticket_id):
            ready.append(state)

    return ready
