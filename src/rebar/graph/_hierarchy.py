"""Hierarchy resolver and archive eligibility for ticket-graph."""

from __future__ import annotations

import os
from typing import Any

from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir

from ..reducer._api import _NON_GRAPH_ARTIFACT_TYPES
from ._loader import reduce_all_tickets, reduce_ticket
from ._relations import _BLOCKING_RELATIONS

# Relations whose links represent BLOCKING dependencies (single source of truth in
# ticket_graph._relations). Only these are subject to hierarchy promotion — see
# resolve_hierarchy_link's docstring for rationale.

# Sentinel parent shared by every root ticket. jira-reb-1582 specifies that
# "tickets with no parent are considered siblings for this purpose", so the
# ancestor walk terminates every chain with this: a nearest common ancestor then
# ALWAYS exists and the disjoint-tree case needs no separate branch. It is never a
# resolved endpoint — resolution returns the element PRECEDING the common ancestor.
# The \x00 prefix keeps it disjoint from any real ticket id.
_VIRTUAL_ROOT = "\x00virtual-root"


def _get_ancestors(ticket_id: str, tracker_dir: str) -> list[str]:
    """Return ticket_id's ancestor chain, self first, terminated by _VIRTUAL_ROOT.

    Walks all the way to the real root. There is deliberately no hop cap: the
    former ``max_hops=2`` truncated any chain deeper than epic→story→task, which
    made resolution fall back to the wrong ancestor (bug 1803-df54-18bb-4881).
    The ``seen`` set makes a malformed parent cycle terminate instead of looping.
    """
    chain: list[str] = [ticket_id]
    seen: set[str] = {ticket_id}
    current = ticket_id
    while True:
        ticket_dir = layout_ticket_dir(tracker_dir, current)
        if not os.path.isdir(ticket_dir):
            break
        try:
            state = reduce_ticket(ticket_dir)
        except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible ancestor stops the hierarchy walk
            state = None
        if state is None:
            break
        parent_id = state.get("parent_id")
        if not parent_id or parent_id in seen:
            break
        chain.append(parent_id)
        seen.add(parent_id)
        current = parent_id
    chain.append(_VIRTUAL_ROOT)
    return chain


def _resolve_blocking_endpoints(
    source_chain: list[str], target_chain: list[str]
) -> tuple[str, str, bool]:
    """Resolve a blocking pair to comparable endpoints via their nearest common ancestor.

    Both arguments are ``_get_ancestors`` output, so both terminate in
    ``_VIRTUAL_ROOT`` and a common ancestor is guaranteed to exist.

    Returns ``(resolved_source, resolved_target, is_ancestor_pair)``:

      - When the common ancestor IS one of the endpoints, that endpoint is an
        ancestor of the other. There is no valid escalation — a ticket must never
        block its own subtree — so the pair comes back unchanged with
        ``is_ancestor_pair`` True and the caller rejects it.
      - Otherwise each endpoint escalates to its own ancestor sitting directly below
        the common ancestor: the element immediately preceding it in that chain. An
        endpoint already at that level is returned unchanged, so two children of one
        parent link exactly as requested.

    The two escalated endpoints are always distinct, and always siblings rather than
    an ancestor pair: if both escalated to the same child of the common ancestor,
    that child would itself appear in both chains ahead of it, contradicting it
    being the NEAREST common ancestor.
    """
    target_index = {tid: i for i, tid in enumerate(target_chain)}
    source_id, target_id = source_chain[0], target_chain[0]

    # A self-loop is NOT an ancestor pair — a ticket is not its own ancestor. Pass
    # it through untouched so add_dependency's cycle guard still raises
    # CyclicDependencyError for it rather than the redundant-link ValueError.
    if source_id == target_id:
        return source_id, target_id, False

    nca_source_index = next(i for i, tid in enumerate(source_chain) if tid in target_index)
    nca = source_chain[nca_source_index]

    if nca in (source_id, target_id):
        return source_id, target_id, True

    return source_chain[nca_source_index - 1], target_chain[target_index[nca] - 1], False


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
        STRUCTURALLY — by position in the parent hierarchy, never by ticket type.
        Any two tickets sharing a parent may hold a dependency and are linked as
        given; otherwise each endpoint is escalated to its own ancestor that is a
        child of the two endpoints' nearest common ancestor. Tickets with no
        parent count as siblings of each other (see ``_VIRTUAL_ROOT``), so two
        roots link unchanged while a deep leaf linked across trees escalates to
        its own root. An endpoint that is an ancestor of the other cannot be
        escalated at all and is reported via ``is_redundant``.

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
    source_dir = layout_ticket_dir(tracker_dir, source_id)
    target_dir = layout_ticket_dir(tracker_dir, target_id)

    if not os.path.isdir(source_dir):
        return {"error": f"ticket '{source_id}' does not exist", "ticket_id": source_id}
    if not os.path.isdir(target_dir):
        return {"error": f"ticket '{target_id}' does not exist", "ticket_id": target_id}

    try:
        source_state = reduce_ticket(source_dir)
    except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible source is reported as not-reducible
        source_state = None
    if source_state is None:
        return {
            "error": f"ticket '{source_id}' could not be reduced",
            "ticket_id": source_id,
        }

    try:
        target_state = reduce_ticket(target_dir)
    except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible target is reported as not-reducible
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

    # ── non-graph artifact endpoints never participate in blocking links. ─────
    # A session_log / code_review is a leaf, lifecycle-exempt artifact excluded from
    # the dependency graph; a blocks/depends_on to or from one is refused. (The
    # non-blocking relations relates_to / duplicates / supersedes / discovered_from
    # are permitted — they return at the early non-blocking branch above.)
    if source_state.get("ticket_type") in _NON_GRAPH_ARTIFACT_TYPES:
        _kind = source_state.get("ticket_type")
        return {
            "error": f"ticket '{source_id}' is a {_kind} and cannot be a "
            f"'{relation}' (blocking) link endpoint; use relates_to/discovered_from",
            "ticket_id": source_id,
        }
    if target_state.get("ticket_type") in _NON_GRAPH_ARTIFACT_TYPES:
        _kind = target_state.get("ticket_type")
        return {
            "error": f"ticket '{target_id}' is a {_kind} and cannot be a "
            f"'{relation}' (blocking) link endpoint; use relates_to/discovered_from",
            "ticket_id": target_id,
        }

    # ── Blocking relations: enforce STRUCTURAL comparability. ─────────────────
    # Escalate each endpoint to the nearest common ancestor's children, so the
    # recorded dependency is between siblings. Replaces the former type-tier rule,
    # under which a task and a story that were both children of one epic could not
    # link directly and the escalation instead landed on the epic — leaving it
    # blocked by its own child (bug 1803-df54-18bb-4881, story affe-2b42-4ee4-4e12).
    source_chain = _get_ancestors(source_id, tracker_dir)
    target_chain = _get_ancestors(target_id, tracker_dir)

    resolved_source, resolved_target, is_ancestor_pair = _resolve_blocking_endpoints(
        source_chain, target_chain
    )

    # is_redundant is RECOMPUTED from the RESOLVED pair. The original-pair value
    # above still serves the non-blocking early return, whose semantics are
    # unchanged; but for a blocking link what matters is the edge actually
    # WRITTEN, so an escalation landing on an ancestor of the other endpoint is
    # caught here rather than slipping past a guard that only saw the
    # pre-escalation pair (the second half of bug 1803-df54-18bb-4881).
    return {
        "resolved_source": resolved_source,
        "resolved_target": resolved_target,
        "was_redirected": (resolved_source != source_id) or (resolved_target != target_id),
        "is_redundant": is_ancestor_pair,
    }


def compute_archive_eligible(tracker_dir: str) -> list[str]:
    """Return closed ticket IDs eligible for archival.

    A closed ticket is eligible if it is NOT reachable from any open ticket
    via depends_on or blocks edges (traversed bidirectionally), and is not
    already archived.
    """
    all_tickets = reduce_all_tickets(tracker_dir, exclude_archived=False, exclude_session_logs=True)

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
