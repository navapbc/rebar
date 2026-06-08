#!/usr/bin/env python3
"""ticket-list-descendants: BFS walk from a root ticket ID, bucketed by type.

Accepts TICKETS_TRACKER_DIR env var (or derives from git root).
Accepts a ticket_id as first positional argument.

Output JSON schema:
{
  "epics":               ["id", ...],
  "stories":             ["id", ...],
  "tasks":               ["id", ...],
  "bugs":                ["id", ...],
  "parents_with_children": ["id", ...]   # tickets in the tree that themselves have children
}

All arrays are empty when the root has no descendants (or when the root does not exist).
Exit 0 on success; exit 1 on usage error.
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from ticket_reducer import reduce_all_tickets  # noqa: E402


def _find_tracker_dir() -> str:
    """Return the .tickets-tracker directory path."""
    if os.environ.get("TICKETS_TRACKER_DIR"):
        return os.environ["TICKETS_TRACKER_DIR"]
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        repo_root = result.stdout.strip()
        return os.path.join(repo_root, ".tickets-tracker")
    except Exception:
        return os.path.join(os.getcwd(), ".tickets-tracker")


def _load_all_states(tracker_dir: str) -> dict[str, dict]:
    """Load reduced state for every ticket in tracker_dir.

    Returns a mapping of ticket_id -> state dict.
    Delegates to reduce_all_tickets so future reducer changes apply here too.
    """
    if not os.path.isdir(tracker_dir):
        return {}
    states: dict[str, dict] = {}
    for state in reduce_all_tickets(tracker_dir):
        if state.get("status") in ("error", "fsck_needed"):
            continue
        tid = state.get("ticket_id")
        if tid:
            states[tid] = state
    return states


def list_descendants(root_id: str, tracker_dir: str) -> dict:
    """BFS walk from root_id; bucket descendants by ticket_type.

    Returns the output dict (always valid, with empty arrays when no descendants
    or when root_id is not found in the store).
    """
    all_states = _load_all_states(tracker_dir)

    # Map ticket_type singular -> bucket key (handles irregular plurals)
    _TYPE_TO_BUCKET: dict[str, str] = {
        "epic": "epics",
        "story": "stories",
        "task": "tasks",
        "bug": "bugs",
    }

    buckets: dict[str, list[str]] = {
        "epics": [],
        "stories": [],
        "tasks": [],
        "bugs": [],
    }
    parents_with_children: list[str] = []

    # BFS
    visited: set[str] = set()
    queue: deque[str] = deque([root_id])
    visited.add(root_id)

    # Pre-build a parent_id -> [child_ids] index over all states
    children_of: dict[str, list[str]] = {}
    for tid, state in all_states.items():
        pid = state.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(tid)

    while queue:
        current_id = queue.popleft()
        children = children_of.get(current_id, [])

        if children:
            parents_with_children.append(current_id)

        for child_id in children:
            if child_id in visited:
                continue
            visited.add(child_id)
            queue.append(child_id)

            child_state = all_states.get(child_id)
            ttype = child_state.get("ticket_type") if child_state else None
            bucket_key = _TYPE_TO_BUCKET.get(ttype) if ttype else None
            if bucket_key:
                buckets[bucket_key].append(child_id)
            # tickets with unknown/None type are not bucketed but still traversed

    # The root is included in parents_with_children when it has children,
    # because it is an interior node in the traversal like any other parent.

    return {
        "epics": buckets["epics"],
        "stories": buckets["stories"],
        "tasks": buckets["tasks"],
        "bugs": buckets["bugs"],
        "parents_with_children": parents_with_children,
    }


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: ticket-list-descendants.py <ticket_id>", file=sys.stderr)
        return 1

    raw_root_id = args[0]
    tracker_dir = _find_tracker_dir()

    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from ticket_resolver import resolve_ticket_id

    # list_descendants returns empty arrays gracefully when root_id is not in
    # the store (documented contract). Pass the raw input through when
    # resolution finds no match so that behaviour is preserved.
    root_id = resolve_ticket_id(raw_root_id, tracker_dir) or raw_root_id

    result = list_descendants(root_id, tracker_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
