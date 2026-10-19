"""Parent-chain descendants of a ticket (``rebar deps <id> --descendants``).

BFS walk from a root ticket, bucketed by type, reusing ``rebar.reducer.reduce_all_tickets``.
The ``list-descendants`` verb was removed; the CLI reads this through ``deps --descendants``.

Output (always valid, empty arrays when the root has no descendants / is absent):
``{epics, stories, tasks, bugs, parents_with_children}``.
"""

from __future__ import annotations

from collections import deque

from rebar.reducer import reduce_all_tickets

_TYPE_TO_BUCKET = {"epic": "epics", "story": "stories", "task": "tasks", "bug": "bugs"}


def _load_all_states(tracker: str) -> dict[str, dict]:
    import os

    if not os.path.isdir(tracker):
        return {}
    states: dict[str, dict] = {}
    for state in reduce_all_tickets(tracker):
        if state.get("status") in ("error", "fsck_needed"):
            continue
        tid = state.get("ticket_id")
        if tid:
            states[tid] = state
    return states


def list_descendants(root_id: str, tracker: str) -> dict:
    """BFS from ``root_id``; bucket descendants by ``ticket_type``."""
    all_states = _load_all_states(tracker)
    buckets: dict[str, list[str]] = {"epics": [], "stories": [], "tasks": [], "bugs": []}
    parents_with_children: list[str] = []

    children_of: dict[str, list[str]] = {}
    for tid, state in all_states.items():
        pid = state.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(tid)

    visited: set[str] = {root_id}
    queue: deque[str] = deque([root_id])
    while queue:
        current = queue.popleft()
        children = children_of.get(current, [])
        if children:
            parents_with_children.append(current)
        for child_id in children:
            if child_id in visited:
                continue
            visited.add(child_id)
            queue.append(child_id)
            child_state = all_states.get(child_id)
            ttype = child_state.get("ticket_type") if child_state else None
            bucket = _TYPE_TO_BUCKET.get(ttype) if ttype else None
            if bucket:
                buckets[bucket].append(child_id)

    return {
        "epics": buckets["epics"],
        "stories": buckets["stories"],
        "tasks": buckets["tasks"],
        "bugs": buckets["bugs"],
        "parents_with_children": parents_with_children,
    }
