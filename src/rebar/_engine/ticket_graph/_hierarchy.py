"""Hierarchy resolver and archive eligibility for ticket-graph."""

from __future__ import annotations

import os
from typing import Any

from ticket_graph._loader import reduce_all_tickets, reduce_ticket


def _get_ancestors(ticket_id: str, tracker_dir: str, max_hops: int = 2) -> list[str]:
    """Return the ancestor chain for ticket_id up to max_hops hops."""
    chain: list[str] = [ticket_id]
    current = ticket_id
    for _ in range(max_hops):
        ticket_dir = os.path.join(tracker_dir, current)
        if not os.path.isdir(ticket_dir):
            break
        try:
            state = reduce_ticket(ticket_dir)
        except Exception:
            state = None
        if state is None:
            break
        parent_id = state.get("parent_id")
        if not parent_id:
            break
        chain.append(parent_id)
        current = parent_id
    return chain


def resolve_hierarchy_link(
    source_id: str,
    target_id: str,
    tracker_dir: str,
) -> dict[str, object]:
    """Resolve the effective hierarchy link target for a (source, target) ticket pair.

    Walks each ticket's parent_id chain (≤2 hops) using reduce_ticket, finds the
    divergence point in the hierarchy, and returns a dict:
        {
            "resolved_source": str,   # effective source (may be ancestor)
            "resolved_target": str,   # effective target (may be ancestor)
            "was_redirected": bool,   # True if either ID was redirected to an ancestor
            "is_redundant": bool,     # True if source is direct parent of target or vice versa
        }

    On error (unreadable ticket):
        {"error": str, "ticket_id": str}  with the caller expected to exit non-zero.
    """
    source_dir = os.path.join(tracker_dir, source_id)
    target_dir = os.path.join(tracker_dir, target_id)

    if not os.path.isdir(source_dir):
        return {"error": f"ticket '{source_id}' does not exist", "ticket_id": source_id}
    if not os.path.isdir(target_dir):
        return {"error": f"ticket '{target_id}' does not exist", "ticket_id": target_id}

    try:
        source_state = reduce_ticket(source_dir)
    except Exception:
        source_state = None
    if source_state is None:
        return {
            "error": f"ticket '{source_id}' could not be reduced",
            "ticket_id": source_id,
        }

    try:
        target_state = reduce_ticket(target_dir)
    except Exception:
        target_state = None
    if target_state is None:
        return {
            "error": f"ticket '{target_id}' could not be reduced",
            "ticket_id": target_id,
        }

    source_chain = _get_ancestors(source_id, tracker_dir, max_hops=2)
    target_chain = _get_ancestors(target_id, tracker_dir, max_hops=2)

    source_parent = source_state.get("parent_id")
    target_parent = target_state.get("parent_id")
    is_redundant = (source_id == target_parent) or (target_id == source_parent)

    target_ancestors = set(target_chain)

    shared: str | None = None
    for ancestor in source_chain:
        if ancestor in target_ancestors:
            shared = ancestor
            break

    if shared is None:
        resolved_source = source_chain[-1]
        resolved_target = target_chain[-1]
    else:

        def _last_before(chain: list[str], shared_id: str) -> str:
            idx = chain.index(shared_id)
            return chain[idx - 1] if idx > 0 else chain[0]

        resolved_source = _last_before(source_chain, shared)
        resolved_target = _last_before(target_chain, shared)

    was_redirected = (resolved_source != source_id) or (resolved_target != target_id)

    return {
        "resolved_source": resolved_source,
        "resolved_target": resolved_target,
        "was_redirected": was_redirected,
        "is_redundant": is_redundant,
    }


def compute_archive_eligible(tracker_dir: str) -> list[str]:
    """Return closed ticket IDs eligible for archival.

    A closed ticket is eligible if it is NOT reachable from any open ticket
    via depends_on or blocks edges (traversed bidirectionally), and is not
    already archived.
    """
    all_tickets = reduce_all_tickets(tracker_dir, exclude_archived=False)

    ticket_map: dict[str, dict[str, Any]] = {}
    for t in all_tickets:
        tid = t.get("ticket_id", "")
        if tid:
            ticket_map[tid] = t

    # Build undirected adjacency list for depends_on and blocks edges
    adj: dict[str, set[str]] = {tid: set() for tid in ticket_map}
    for tid, t in ticket_map.items():
        for dep in t.get("deps", []):
            relation = dep.get("relation", "")
            target = dep.get("target_id", "")
            if relation in ("depends_on", "blocks") and target:
                adj.setdefault(tid, set()).add(target)
                adj.setdefault(target, set()).add(tid)

    # Identify open (non-closed, non-archived) tickets as BFS seeds
    seeds: list[str] = []
    for tid, t in ticket_map.items():
        status = t.get("status", "open")
        archived = t.get("archived", False)
        if status != "closed" and not archived:
            seeds.append(tid)

    # BFS from all seeds
    reachable: set[str] = set()
    queue = list(seeds)
    visited: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        reachable.add(current)
        for neighbor in adj.get(current, set()):
            if neighbor not in visited:
                queue.append(neighbor)

    # Eligible: closed, not archived, not reachable
    eligible: list[str] = []
    for tid, t in ticket_map.items():
        status = t.get("status", "open")
        archived = t.get("archived", False)
        if status == "closed" and not archived and tid not in reachable:
            eligible.append(tid)

    return sorted(eligible)
