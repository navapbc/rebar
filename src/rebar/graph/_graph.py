"""Dependency graph building and cycle detection for ticket-graph."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

from rebar.reducer import is_terminal_status

from . import _loader as _loader_module
from ._blockers import _find_direct_blockers
from ._cache import (
    _compute_cache_key,
    _read_graph_cache,
    _write_graph_cache,
)
from ._relations import bfs
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
    ticket_states: dict[str, Any] = _loader_module.load_indexed_states(tracker_dir)

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
        if not is_terminal_status(status):
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


def _reduce_or_none(ticket_dir: str) -> dict[str, Any] | None:
    """Reduce a ticket dir, returning None when it is unreducible or not a dict.

    The fail-open guard the graph traversals share: an unreducible ticket dir is
    skipped rather than aborting the walk.
    """
    try:
        state = reduce_ticket(ticket_dir)
    except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible ticket dir is skipped during traversal
        return None
    return state if isinstance(state, dict) else None


def _blocks_targets(state: dict[str, Any]) -> Iterator[str]:
    """Yield the target of every ``blocks`` dep on ``state``."""
    for dep in state.get("deps", []):
        if dep.get("relation") == "blocks":
            target = dep.get("target_id", "")
            if target:
                yield target


def _dependents_of(tracker_dir: str, current: str, visited: set[str]) -> Iterator[str]:
    """Yield every not-yet-visited ticket dir carrying a ``depends_on`` dep on ``current``."""
    try:
        entries = os.listdir(tracker_dir)
    except OSError:
        return

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
        e_state = _reduce_or_none(entry_path)
        if e_state is None:
            continue
        for dep in e_state.get("deps", []):
            if dep.get("relation") == "depends_on" and dep.get("target_id") == current:
                yield entry


def _get_all_blocked_by(ticket_id: str, tracker_dir: str) -> set[str]:
    """Return the set of all tickets (transitively) blocked by ticket_id.

    The result is the ACCUMULATOR of discovered edge targets, not the BFS visited
    set: a ``blocks`` target is included even when no ticket directory backs it,
    and ``ticket_id`` itself is excluded unless a real edge points back at it.
    ``check_would_create_cycle`` depends on both halves of that.
    """

    def neighbors(current: str, visited: set[str]) -> Iterator[str]:
        current_dir = os.path.join(tracker_dir, current)
        if os.path.isdir(current_dir):
            state = _reduce_or_none(current_dir)
            if state is not None:
                yield from _blocks_targets(state)
        yield from _dependents_of(tracker_dir, current, visited)

    return bfs([ticket_id], neighbors)


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
    if relation in ("relates_to", "duplicates", "supersedes", "discovered_from", "caused_by"):
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


def _is_at_level(tracker_dir: str, ticket_id: str, level: str) -> bool:
    """True when ``ticket_id`` has a backing dir reducing to the given ``ticket_type``.

    A missing or unreducible dir is not at any level, so it is never traversed.
    """
    ticket_dir = os.path.join(tracker_dir, ticket_id)
    if not os.path.isdir(ticket_dir):
        return False
    state = _reduce_or_none(ticket_dir)
    return state is not None and state.get("ticket_type", "").lower() == level


def _same_level_neighbors(tracker_dir: str, level: str) -> Callable[[str, set[str]], Iterator[str]]:
    """Build the ``neighbors_fn`` for a cycle walk confined to one ticket level.

    Both ends of an edge must sit at ``level``: a node of another type is not
    expanded, and a target of another type is not traversed into.
    """

    def neighbors(current: str, visited: set[str]) -> Iterator[str]:
        current_dir = os.path.join(tracker_dir, current)
        if not os.path.isdir(current_dir):
            return
        state = _reduce_or_none(current_dir)
        if state is None or state.get("ticket_type", "").lower() != level:
            return
        for dep in state.get("deps", []):
            if dep.get("relation", "") not in ("blocks", "depends_on"):
                continue
            target = dep.get("target_id", "")
            if target and target not in visited and _is_at_level(tracker_dir, target, level):
                yield target

    return neighbors


def check_cycle_at_level(source_id: str, target_id: str, level: str, tracker_dir: str) -> bool:
    """Return True if adding source_id→target_id would create a cycle at the given level.

    A self-loop (source_id == target_id) always returns True.
    """
    if not level:
        return False

    if source_id == target_id:
        return True

    neighbors = _same_level_neighbors(tracker_dir, level)

    # Walking to completion and testing membership is equivalent to the former
    # mid-loop ``if current == source_id: return True``: source_id can only be
    # dequeued if some neighbour yielded it (the seed is target_id, and the
    # self-loop case already returned), so it is reachable iff it is accumulated.
    return source_id in bfs([target_id], neighbors)
