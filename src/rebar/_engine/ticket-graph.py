#!/usr/bin/env python3
"""Ticket graph engine: dependency traversal, cycle detection, ready_to_work, cache.

Reads compiled ticket state via ticket-reducer.py (imported via importlib for the
hyphenated filename) and builds a dependency graph for a given ticket.

Public API:
    build_dep_graph(ticket_id: str, tracker_dir: str) -> dict
    check_would_create_cycle(source_id: str, target_id: str, relation: str,
                             tracker_dir: str) -> bool
    add_dependency(source_id: str, target_id: str, tracker_dir: str,
                   relation: str = "blocks") -> None
    resolve_hierarchy_link(source_id: str, target_id: str,
                           tracker_dir: str) -> dict
    check_cycle_at_level(source_id: str, target_id: str, level: str,
                         tracker_dir: str) -> bool
    CyclicDependencyError (exception class)

CLI:
    python3 ticket-graph.py <ticket_id> [--tickets-dir=<path>]
    python3 ticket-graph.py --link <source> <target> <relation>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure scripts directory is on sys.path so ticket_graph package is importable
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# ---------------------------------------------------------------------------
# Submodule imports — all logic lives in ticket_graph/
# ---------------------------------------------------------------------------

from ticket_graph._graph import (  # noqa: E402
    build_dep_graph,
    check_cycle_at_level,
    check_would_create_cycle,
)
from ticket_graph._hierarchy import compute_archive_eligible, resolve_hierarchy_link  # noqa: E402
from ticket_graph._links import CyclicDependencyError, _is_active_link, add_dependency  # noqa: E402
import ticket_graph._loader as _ticket_graph_loader  # noqa: E402
from ticket_graph._loader import reduce_ticket  # noqa: E402
from ticket_graph._blockers import _find_direct_blockers  # noqa: E402
from ticket_graph._graph import _compute_dep_graph  # noqa: E402

# Module-level aliases for backward compatibility with tests that access these directly
_reduce_ticket = reduce_ticket
# _reducer must be the same object instance used by _graph.py and _blockers.py
# so that test patches on graph._reducer.reduce_all_tickets intercept real calls
_reducer = _ticket_graph_loader.reducer

# Re-export all public symbols for backward compatibility
__all__ = [
    "build_dep_graph",
    "check_cycle_at_level",
    "check_would_create_cycle",
    "resolve_hierarchy_link",
    "compute_archive_eligible",
    "CyclicDependencyError",
    "add_dependency",
    "_is_active_link",
    "_find_direct_blockers",
    "_compute_dep_graph",
]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


from ticket_resolver import resolve_ticket_id as _resolve_ticket_id  # noqa: E402


def main() -> int:
    """CLI entry point."""
    args = sys.argv[1:]

    if not args:
        print(
            "Usage: ticket-graph.py <ticket_id> [--tickets-dir=<path>]\n"
            "       ticket-graph.py --link <source> <target> <relation>",
            file=sys.stderr,
        )
        return 1

    def _find_tracker_dir(args: list[str]) -> tuple[str, list[str]]:
        remaining = []
        tracker_dir = None
        for arg in args:
            if arg.startswith("--tickets-dir="):
                tracker_dir = arg.split("=", 1)[1]
            else:
                remaining.append(arg)
        if tracker_dir is None:
            try:
                import subprocess

                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                repo_root = result.stdout.strip()
                tracker_dir = os.path.join(repo_root, ".tickets-tracker")
            except Exception:
                tracker_dir = os.path.join(os.getcwd(), ".tickets-tracker")
        return tracker_dir, remaining

    if args[0] == "resolve-hierarchy-link":
        remaining = args[1:]
        tracker_dir, pos_args = _find_tracker_dir(remaining)
        if len(pos_args) < 2:
            print(
                "Usage: ticket-graph.py resolve-hierarchy-link <source> <target>"
                " [--tickets-dir=<path>]",
                file=sys.stderr,
            )
            return 1
        raw_source = pos_args[0]
        raw_target = pos_args[1]
        source_id = _resolve_ticket_id(raw_source, tracker_dir)
        if source_id is None:
            print(f"Error: ticket '{raw_source}' does not exist", file=sys.stderr)
            return 1
        target_id = _resolve_ticket_id(raw_target, tracker_dir)
        if target_id is None:
            print(f"Error: ticket '{raw_target}' does not exist", file=sys.stderr)
            return 1
        result = resolve_hierarchy_link(source_id, target_id, tracker_dir)
        print(json.dumps(result, ensure_ascii=False))
        if "error" in result:
            return 1
        return 0

    if args[0] == "--archive-eligible":
        tracker_dir, _ = _find_tracker_dir(args[1:])
        eligible = compute_archive_eligible(tracker_dir)
        print(json.dumps(eligible))
        return 0

    if args[0] == "--link":
        if len(args) < 4:
            print(
                "Usage: ticket-graph.py --link <source> <target> <relation>",
                file=sys.stderr,
            )
            return 1
        raw_source = args[1]
        raw_target = args[2]
        relation = args[3]
        tracker_dir, _ = _find_tracker_dir([])

        source_id = _resolve_ticket_id(raw_source, tracker_dir)
        if source_id is None:
            print(f"Error: ticket '{raw_source}' does not exist", file=sys.stderr)
            return 1
        target_id = _resolve_ticket_id(raw_target, tracker_dir)
        if target_id is None:
            print(f"Error: ticket '{raw_target}' does not exist", file=sys.stderr)
            return 1

        try:
            add_dependency(source_id, target_id, tracker_dir, relation)
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    # Deps query mode
    include_archived = "--include-archived" in args
    if include_archived:
        args = [a for a in args if a != "--include-archived"]

    tracker_dir, remaining_args = _find_tracker_dir(args)

    if not remaining_args:
        print(
            "Usage: ticket-graph.py <ticket_id> [--tickets-dir=<path>]",
            file=sys.stderr,
        )
        return 1

    raw_ticket_id = remaining_args[0]
    ticket_id = _resolve_ticket_id(raw_ticket_id, tracker_dir)
    if ticket_id is None:
        print(f"Error: ticket '{raw_ticket_id}' does not exist", file=sys.stderr)
        return 1

    ticket_dir = os.path.join(tracker_dir, ticket_id)

    if not include_archived:
        try:
            target_state = reduce_ticket(ticket_dir)
        except Exception:
            target_state = None
        if target_state is not None and isinstance(target_state, dict):
            if target_state.get("archived") is True:
                print(
                    f"Error: ticket '{ticket_id}' is archived. "
                    "Use --include-archived to include archived tickets.",
                    file=sys.stderr,
                )
                return 1

    exclude_archived = not include_archived
    result = build_dep_graph(ticket_id, tracker_dir, exclude_archived=exclude_archived)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
