"""Graph exclusion for overlap detection (epic only-crave-art, story 5a8f).

``related_ticket_ids`` returns the set of tickets in a query ticket's OWN graph —
ancestors, descendants, siblings, and linked tickets (both directions) — so Stage-1
candidate generation never surfaces a ticket's already-related work (the top source of
"related-but-distinct" false flags). It loads all ticket states ONCE (the same
``_load_all_states`` helper ``descendants.py`` uses) and derives all four relation sets
from that one dict; it does NOT refactor ``next_batch``.
"""

from __future__ import annotations

from collections import deque


def related_ticket_ids(
    ticket_id: str, tracker: str, *, all_states: dict[str, dict] | None = None
) -> set[str]:
    """The query ticket's own-graph ids to EXCLUDE from candidate generation:
    parents (up the chain), descendants (BFS down ``parent_id``), siblings (same
    ``parent_id`` — a root ticket with no parent has NONE), and tickets linked in EITHER
    direction (this ticket's own ``deps`` targets, plus any ticket whose ``deps`` point at
    this one). Never includes ``ticket_id`` itself. Best-effort: a load/parse error yields
    an empty set (candidate generation then treats nothing as excluded, never raising)."""
    if all_states is None:
        from rebar._engine_support.descendants import _load_all_states

        all_states = _load_all_states(tracker)

    related: set[str] = set()
    me = all_states.get(ticket_id)
    if me is None:
        return related

    # Ancestors: walk parent_id up (cycle-safe).
    seen = {ticket_id}
    cur: dict | None = me
    while cur is not None:
        pid = cur.get("parent_id")
        if not pid or pid in seen:
            break
        seen.add(pid)
        related.add(pid)
        cur = all_states.get(pid)

    # Descendants: BFS down the parent_id tree.
    children_of: dict[str, list[str]] = {}
    for tid, st in all_states.items():
        pid = st.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(tid)
    queue: deque[str] = deque(children_of.get(ticket_id, []))
    while queue:
        child = queue.popleft()
        if child in related or child == ticket_id:
            continue
        related.add(child)
        queue.extend(children_of.get(child, []))

    # Siblings: same parent_id (a root ticket with no parent has no siblings).
    my_parent = me.get("parent_id")
    if my_parent:
        for tid, st in all_states.items():
            if tid != ticket_id and st.get("parent_id") == my_parent:
                related.add(tid)

    # Linked, BOTH directions: this ticket's own deps targets …
    for dep in me.get("deps", []) or []:
        target = dep.get("target_id")
        if target:
            related.add(target)
    # … and any ticket whose deps point AT this one (incoming links are not in our deps).
    for tid, st in all_states.items():
        for dep in st.get("deps", []) or []:
            if dep.get("target_id") == ticket_id:
                related.add(tid)

    related.discard(ticket_id)
    return related
