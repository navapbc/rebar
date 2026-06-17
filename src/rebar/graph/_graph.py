"""Dependency graph building and cycle detection for ticket-graph."""

from __future__ import annotations

import os
from typing import Any

from . import _loader as _loader_module
from ._blockers import _find_direct_blockers
from ._cache import (
    _compute_cache_key,
    _read_graph_cache,
    _write_graph_cache,
)
from ._status import _get_ticket_status

# Use module-level accessor so tests can patch _loader_module.reducer.reduce_all_tickets
reduce_ticket = _loader_module.reduce_ticket


def build_dep_graph(
    ticket_id: str, tracker_dir: str, exclude_archived: bool = True
) -> dict[str, Any]:
    """Build the dependency graph for a ticket.

    Returns:
        {
            "ticket_id": str,
            "deps": list[dict],   # raw dep entries from compiled state
            "blockers": list[str], # ticket IDs that directly block this ticket
            "children": list[str], # ticket IDs whose parent_id == ticket_id
            "ready_to_work": bool, # True when all direct blockers are closed/tombstoned
        }

    Uses a graph cache keyed by content hash of all ticket dirs.

    Args:
        exclude_archived: When True (default), archived tickets are excluded from
            children and blockers lists. Pass False to include archived tickets.
    """
    cache_key = _compute_cache_key(tracker_dir)

    # Only use cache for default (exclude_archived=True) to avoid stale results
    if cache_key and exclude_archived:
        cached_graphs = _read_graph_cache(tracker_dir, cache_key)
        if cached_graphs is not None and ticket_id in cached_graphs:
            return cached_graphs[ticket_id]

    result = _compute_dep_graph(ticket_id, tracker_dir, exclude_archived=exclude_archived)

    if cache_key and exclude_archived:
        cached_graphs = _read_graph_cache(tracker_dir, cache_key) or {}
        cached_graphs[ticket_id] = result
        _write_graph_cache(tracker_dir, cache_key, cached_graphs)

    return result


def _compute_dep_graph(
    ticket_id: str, tracker_dir: str, exclude_archived: bool = True
) -> dict[str, Any]:
    """Compute (without cache) the dependency graph for ticket_id."""
    all_states_list = _loader_module.reducer.reduce_all_tickets(
        tracker_dir, exclude_archived=False, exclude_session_logs=True
    )
    ticket_states: dict[str, Any] = {}
    for t in all_states_list:
        tid = t.get("ticket_id", "")
        if tid and t.get("status") not in ("error", "fsck_needed"):
            ticket_states[tid] = t

    deps: list[dict[str, Any]] = []
    state = ticket_states.get(ticket_id)
    if state is None:
        # The queried ticket is absent from the (session-log-excluded) node set when
        # it is itself a session_log. Logs are excluded as *graph nodes* of other
        # tickets, but `deps <session_log>` must still surface the log's own
        # non-blocking links (relates_to / discovered_from) — reduce it singly.
        ticket_dir = os.path.join(tracker_dir, ticket_id)
        if os.path.isdir(ticket_dir):
            single = reduce_ticket(ticket_dir)
            if single is not None and isinstance(single, dict):
                state = single
    if state is not None and isinstance(state, dict):
        deps = list(state.get("deps", []))

    direct_blockers = _find_direct_blockers(
        ticket_id,
        tracker_dir,
        exclude_archived=exclude_archived,
        ticket_states=ticket_states,
    )

    children: list[str] = []
    for entry, child_state in ticket_states.items():
        if entry == ticket_id:
            continue
        if child_state is not None and isinstance(child_state, dict):
            if child_state.get("parent_id") == ticket_id:
                if exclude_archived and child_state.get("archived") is True:
                    continue
                children.append(entry)

    ready_to_work = True
    for blocker_id in direct_blockers:
        status = _get_ticket_status(blocker_id, tracker_dir)
        if status not in ("closed", "deleted"):
            ready_to_work = False
            break

    return {
        "ticket_id": ticket_id,
        "deps": deps,
        "blockers": direct_blockers,
        "children": children,
        "ready_to_work": ready_to_work,
    }


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def _get_all_blocked_by(ticket_id: str, tracker_dir: str) -> set[str]:
    """Return the set of all tickets (transitively) blocked by ticket_id."""
    blocked: set[str] = set()
    queue: list[str] = [ticket_id]
    visited: set[str] = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        current_dir = os.path.join(tracker_dir, current)
        if os.path.isdir(current_dir):
            try:
                state = reduce_ticket(current_dir)
            except Exception:
                state = None

            if state is not None and isinstance(state, dict):
                for dep in state.get("deps", []):
                    if dep.get("relation") == "blocks":
                        target = dep.get("target_id", "")
                        if target:
                            blocked.add(target)
                            if target not in visited:
                                queue.append(target)

        try:
            entries = os.listdir(tracker_dir)
        except OSError:
            entries = []

        for entry in entries:
            if entry in visited:
                continue
            # Skip hidden directories (.suggestions, .review-events, .index, etc.)
            # — they are not ticket dirs and their JSON files are not ticket events.
            if entry.startswith("."):
                continue
            entry_path = os.path.join(tracker_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            try:
                e_state = reduce_ticket(entry_path)
            except Exception:
                e_state = None
            if e_state is None or not isinstance(e_state, dict):
                continue
            for dep in e_state.get("deps", []):
                if dep.get("relation") == "depends_on" and dep.get("target_id") == current:
                    blocked.add(entry)
                    if entry not in visited:
                        queue.append(entry)

    return blocked


def check_would_create_cycle(
    source_id: str, target_id: str, relation: str, tracker_dir: str
) -> bool:
    """Return True if adding source_id→target_id would create a cycle.

    Only 'blocks' and 'depends_on' relations can create cycles.
    'relates_to', 'duplicates', 'supersedes', and 'discovered_from' never create
    cycles and always return False.

    Cycle semantics:
    - ``source blocks target``  means source must precede target.  A cycle
      exists if target already (transitively) precedes source, i.e.
      source ∈ _get_all_blocked_by(target).
    - ``source depends_on target`` means target must precede source.  A cycle
      exists if source already (transitively) precedes target, i.e.
      target ∈ _get_all_blocked_by(source).

    Swapping source/target for depends_on prevents the false-positive where a
    redundant transitive edge A→C→B plus proposed A→B is mis-reported as a
    cycle because A happens to be "blocked by" B in the reverse sense.
    """
    if relation in ("relates_to", "duplicates", "supersedes", "discovered_from"):
        return False

    if relation == "depends_on":
        # source depends_on target ≡ target must precede source.
        # Cycle iff target is already reachable from source in the
        # "must precede" graph, i.e. target ∈ _get_all_blocked_by(source).
        blocked_by_source = _get_all_blocked_by(source_id, tracker_dir)
        return target_id in blocked_by_source
    else:
        # source blocks target ≡ source must precede target.
        # Cycle iff source is already reachable from target in the
        # "must precede" graph, i.e. source ∈ _get_all_blocked_by(target).
        blocked_by_target = _get_all_blocked_by(target_id, tracker_dir)
        return source_id in blocked_by_target


def check_cycle_at_level(source_id: str, target_id: str, level: str, tracker_dir: str) -> bool:
    """Return True if adding source_id→target_id would create a cycle at the given level.

    A self-loop (source_id == target_id) always returns True.
    """
    if not level:
        return False

    if source_id == target_id:
        return True

    visited: set[str] = set()
    queue: list[str] = [target_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if current == source_id:
            return True

        current_dir = os.path.join(tracker_dir, current)
        if not os.path.isdir(current_dir):
            continue

        try:
            state = reduce_ticket(current_dir)
        except Exception:
            continue

        if state is None or not isinstance(state, dict):
            continue

        current_level = state.get("ticket_type", "").lower()
        if current_level != level:
            continue

        for dep in state.get("deps", []):
            relation = dep.get("relation", "")
            if relation in ("blocks", "depends_on"):
                target = dep.get("target_id", "")
                if target and target not in visited:
                    target_dir = os.path.join(tracker_dir, target)
                    if os.path.isdir(target_dir):
                        try:
                            t_state = reduce_ticket(target_dir)
                        except Exception:
                            t_state = None
                        if t_state and t_state.get("ticket_type", "").lower() == level:
                            queue.append(target)

    return False
