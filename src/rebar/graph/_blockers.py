"""Direct-blocker discovery for ticket-graph."""

from __future__ import annotations

from typing import Any

from . import _loader as _loader_module
from ._status import _BLOCKING_RELATIONS


def _is_archived(state: Any) -> bool:
    """True when ``state`` is a compiled state dict flagged archived."""
    return isinstance(state, dict) and state.get("archived") is True


def _own_depends_on_blockers(
    ticket_id: str,
    ticket_states: dict[str, Any],
    exclude_archived: bool,
) -> list[str]:
    """Source 1: the targets of ticket_id's own ``depends_on`` deps.

    ``ticket_id depends_on target`` means target blocks ticket_id. This covers only
    the ``depends_on`` half of ``_BLOCKING_RELATIONS``; the ``blocks`` half points
    outward from this ticket and is Source 2's job (``_blocks_targeting``).
    """
    state = ticket_states.get(ticket_id)
    if not isinstance(state, dict):
        return []

    blockers: list[str] = []
    for dep in state.get("deps", []):
        relation = dep.get("relation")
        if relation not in _BLOCKING_RELATIONS or relation != "depends_on":
            continue
        target = dep.get("target_id", "")
        if not target or target in blockers:
            continue
        if exclude_archived and _is_archived(ticket_states.get(target)):
            continue
        blockers.append(target)
    return blockers


def _blocks_targeting(
    ticket_id: str,
    ticket_states: dict[str, Any],
    exclude_archived: bool,
) -> list[str]:
    """Source 2: every OTHER ticket carrying a ``blocks`` dep aimed at ticket_id."""
    blockers: list[str] = []
    for entry, entry_state in ticket_states.items():
        if entry == ticket_id or not isinstance(entry_state, dict):
            continue
        if exclude_archived and entry_state.get("archived") is True:
            continue
        if any(
            dep.get("relation") == "blocks" and dep.get("target_id") == ticket_id
            for dep in entry_state.get("deps", [])
        ):
            blockers.append(entry)
    return blockers


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
        ticket_states = _loader_module.load_indexed_states(tracker_dir)

    blockers = _own_depends_on_blockers(ticket_id, ticket_states, exclude_archived)
    blockers.extend(
        entry
        for entry in _blocks_targeting(ticket_id, ticket_states, exclude_archived)
        if entry not in blockers
    )
    return blockers
