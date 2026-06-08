"""Direct-blocker discovery for ticket-graph."""

from __future__ import annotations

from typing import Any

import ticket_graph._loader as _loader_module

from ticket_graph._status import _BLOCKING_RELATIONS


def _find_direct_blockers(
    ticket_id: str,
    tracker_dir: str,
    exclude_archived: bool = True,
    ticket_states: dict[str, Any] | None = None,
) -> list[str]:
    """Return a list of ticket IDs that directly block ticket_id.

    Two sources of blocking relations:
    1. ticket_id's own deps with relation == 'depends_on':
       ticket_id depends on these tickets → they block it.
    2. Other tickets' deps with relation == 'blocks' and target_id == ticket_id:
       those tickets block ticket_id.

    Args:
        exclude_archived: When True (default), skip blockers whose compiled state
            has state.get('archived') == True.
        ticket_states: Optional pre-loaded dict keyed by ticket_id with compiled
            state dicts. When provided, avoids per-ticket reduce_ticket calls.
            When None, loads all ticket states via reduce_all_tickets.
    """
    if ticket_states is None:
        all_states = _loader_module.reducer.reduce_all_tickets(
            tracker_dir, exclude_archived=False
        )
        ticket_states = {}
        for t in all_states:
            tid = t.get("ticket_id", "")
            if tid and t.get("status") not in ("error", "fsck_needed"):
                ticket_states[tid] = t

    blockers: list[str] = []

    # Source 1: ticket_id's own compiled deps for 'depends_on'
    state = ticket_states.get(ticket_id)
    if state is not None and isinstance(state, dict):
        for dep in state.get("deps", []):
            if dep.get("relation") in _BLOCKING_RELATIONS:
                if dep.get("relation") == "depends_on":
                    target = dep.get("target_id", "")
                    if target and target not in blockers:
                        if exclude_archived:
                            target_state = ticket_states.get(target)
                            if (
                                target_state is not None
                                and isinstance(target_state, dict)
                                and target_state.get("archived") is True
                            ):
                                continue
                        blockers.append(target)

    # Source 2: scan all ticket states for deps with relation=='blocks'
    # targeting ticket_id
    for entry, entry_state in ticket_states.items():
        if entry == ticket_id:
            continue

        if entry_state is None or not isinstance(entry_state, dict):
            continue

        if exclude_archived and entry_state.get("archived") is True:
            continue

        for dep in entry_state.get("deps", []):
            if dep.get("relation") == "blocks" and dep.get("target_id") == ticket_id:
                if entry not in blockers:
                    blockers.append(entry)
                break  # Only need to add entry once

    return blockers
