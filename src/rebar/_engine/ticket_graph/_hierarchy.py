"""Hierarchy resolver and archive eligibility for ticket-graph."""

from __future__ import annotations

import os
from typing import Any

from ticket_graph._loader import reduce_all_tickets, reduce_ticket
from ticket_graph._relations import _BLOCKING_RELATIONS

# Relations whose links represent BLOCKING dependencies (single source of truth in
# ticket_graph._relations). Only these are subject to hierarchy promotion — see
# resolve_hierarchy_link's docstring for rationale.

# Type-tier mapping defining "comparable level" in the hierarchy. Higher number
# == higher tier. epic (top) > story (mid) > task/bug (leaf, SAME tier).
#   - epic  -> 2
#   - story -> 1
#   - task  -> 0   (leaf)
#   - bug   -> 0   (leaf — bugs are leaf work items, comparable to tasks)
# Anything unrecognized is treated as a leaf (tier 0) so it never spuriously
# out-ranks an epic/story and never gets promoted ABOVE its real ancestors.
_TYPE_TIER: dict[str, int] = {"epic": 2, "story": 1, "task": 0, "bug": 0}


def _tier_of(ticket_type: str | None) -> int:
    """Return the hierarchy tier for a ticket type (leaf=0 for unknown/None)."""
    return _TYPE_TIER.get((ticket_type or "").lower(), 0)


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


def _tier_of_ticket(ticket_id: str, tracker_dir: str) -> int:
    """Return the tier of a ticket id by reducing it (leaf=0 if unreadable)."""
    ticket_dir = os.path.join(tracker_dir, ticket_id)
    if not os.path.isdir(ticket_dir):
        return 0
    try:
        state = reduce_ticket(ticket_dir)
    except Exception:
        state = None
    if state is None:
        return 0
    return _tier_of(state.get("ticket_type"))


def _promote_to_tier(
    chain: list[str], target_tier: int, tracker_dir: str
) -> tuple[str, bool]:
    """Promote the head of ``chain`` UP to the nearest ancestor whose tier matches
    ``target_tier``.

    ``chain`` is [self, parent, grandparent, ...] (output of _get_ancestors).
    Returns ``(resolved_id, was_promoted)``:
      - If the head already sits at target_tier, returns it unchanged.
      - Otherwise walks UP the chain for the first ancestor at exactly target_tier.
      - Fallback: if no ancestor matches the target tier, returns the highest
        available ancestor (chain root) — preserving the historical chain-root
        behavior — and reports was_promoted if that differs from the head.
    """
    head = chain[0]
    for ancestor in chain:
        if _tier_of_ticket(ancestor, tracker_dir) == target_tier:
            return ancestor, ancestor != head
    # No comparable-tier ancestor: fall back to the chain root (highest ancestor).
    root = chain[-1]
    return root, root != head


def resolve_hierarchy_link(
    source_id: str,
    target_id: str,
    tracker_dir: str,
    relation: str = "blocks",
) -> dict[str, object]:
    """Resolve the effective hierarchy link endpoints for a (source, target) pair.

    Semantic model (deliberate change from the old "shared-ancestor" resolver):

      * Promotion ONLY applies to BLOCKING dependencies (``blocks`` /
        ``depends_on``). For every other relation (``relates_to``,
        ``duplicates``, ``supersedes``, ``discovered_from``) the link is created
        between the EXACT source/target the user passed — ``was_redirected`` is
        always False.

      * Blocking dependencies must connect tickets at a COMPARABLE LEVEL, defined
        by ticket TYPE TIER: epic(2) > story(1) > task/bug(0). When the two
        endpoints differ in tier, the LOWER-tier endpoint is promoted UP its
        parent chain to the nearest ancestor whose tier matches the HIGHER-tier
        endpoint, so the resulting link is epic↔epic, story↔story or
        task/bug↔task/bug. If no comparable-tier ancestor exists, fall back to
        the chain root (highest ancestor) and still report was_redirected.

    ``relation`` defaults to ``"blocks"`` so the standalone ``resolve-hierarchy-link``
    CLI subcommand (which carries no relation) still exercises the promotion path.

    Returns:
        {
            "resolved_source": str,   # effective source (may be an ancestor)
            "resolved_target": str,   # effective target (may be an ancestor)
            "was_redirected": bool,   # True if either id was promoted
            "is_redundant": bool,     # True if source is direct parent of target/vice versa
        }
    On error (missing/unreadable ticket):
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

    # is_redundant guard is relation-independent: a direct parent↔child pair is
    # always a redundant link (the hierarchy edge already expresses it).
    source_parent = source_state.get("parent_id")
    target_parent = target_state.get("parent_id")
    is_redundant = (source_id == target_parent) or (target_id == source_parent)

    # ── Non-blocking relations: never promote. Link the exact pair. ───────────
    if relation not in _BLOCKING_RELATIONS:
        return {
            "resolved_source": source_id,
            "resolved_target": target_id,
            "was_redirected": False,
            "is_redundant": is_redundant,
        }

    # ── Blocking relations: enforce type-tier comparability. ──────────────────
    # Promote the lower-tier endpoint up to the higher-tier endpoint's tier so
    # the resulting blocking link is between comparable levels.
    source_chain = _get_ancestors(source_id, tracker_dir, max_hops=2)
    target_chain = _get_ancestors(target_id, tracker_dir, max_hops=2)

    source_tier = _tier_of(source_state.get("ticket_type"))
    target_tier = _tier_of(target_state.get("ticket_type"))

    resolved_source = source_id
    resolved_target = target_id

    if source_tier == target_tier:
        # Same tier already comparable — link as-is (e.g. task↔task siblings,
        # cousins, or unrelated leaves: no promotion).
        pass
    elif source_tier < target_tier:
        # Source is lower: promote it up to the target's (higher) tier.
        resolved_source, _ = _promote_to_tier(
            source_chain, target_tier, tracker_dir
        )
    else:
        # Target is lower: promote it up to the source's (higher) tier.
        resolved_target, _ = _promote_to_tier(
            target_chain, source_tier, tracker_dir
        )

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
