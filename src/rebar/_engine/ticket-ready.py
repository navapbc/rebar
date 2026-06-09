#!/usr/bin/env python3
"""Bulk scan all tickets to identify which are ready to work on.

A ticket is "ready" if:
  1. Its status is "open" or "in_progress"
  2. All direct blocker tickets (deps with relation "depends_on" or "blocks")
     are "closed" (or do not exist / tombstoned)

Usage:
    python3 ticket-ready.py [--format=llm] [--json] [--epic=<epic_id>]

Environment:
    TICKETS_TRACKER_DIR — path to the tickets tracker directory.
    When absent, derived from `git rev-parse --show-toplevel`.

Output (default):       one ticket ID per line
Output (--format=llm):  one JSON object per line (JSONL), LLM-optimised format
Output (--json):        a single JSON ARRAY of compiled ticket-state dicts
                        (the same element shape `list`/`search` emit, derived
                        from the same reducer data path); `--json` wins over
                        `--format` when both are passed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ticket_reducer import reduce_all_tickets  # noqa: E402

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

_OPEN_STATUSES = {"open", "in_progress"}
_CLOSED_STATUSES = {"closed"}
_BLOCKING_RELATIONS = {"blocks", "depends_on"}


def _is_closed(status: str) -> bool:
    return status in _CLOSED_STATUSES


def _is_open(status: str) -> bool:
    return status in _OPEN_STATUSES


def find_ready_tickets(
    tracker_dir: str,
    epic_filter: str | None = None,
) -> list[dict]:
    """Return list of ticket state dicts that are ready to work on.

    A ticket is ready when:
    - status is "open" or "in_progress"
    - all direct blockers are closed (or tombstoned / missing)
    - if epic_filter is set, ticket's parent_id must equal epic_filter

    Args:
        tracker_dir: Path to the .tickets-tracker directory.
        epic_filter: Optional epic ID to scope results to direct children.

    Returns:
        List of ticket state dicts (ready tickets only).
    """
    tracker_path = Path(tracker_dir)
    if not tracker_path.is_dir():
        return []

    all_states_list = reduce_all_tickets(str(tracker_dir))

    # Build a lookup dict, skipping error states.
    ticket_states: dict[str, dict] = {}
    for state in all_states_list:
        tid = state.get("ticket_id")
        if not tid:
            continue
        if state.get("status") in ("error", "fsck_needed"):
            continue
        ticket_states[tid] = state

    # Build blocked_by map: blocked_id → set of blocker_ids
    # deps list in a state: each dep has {target_id, relation, link_uuid}
    # LINK event in ticket X's dir means "X relation target_id"
    # - "depends_on": X depends on target_id → target_id blocks X → blocker=target_id, blocked=X
    # - "blocks":     X blocks target_id → blocker=X, blocked=target_id
    blocked_by: dict[str, set[str]] = {}
    for ticket_id, state in ticket_states.items():
        for dep in state.get("deps", []):
            relation = dep.get("relation")
            if relation not in _BLOCKING_RELATIONS:
                continue
            target_id = dep.get("target_id")
            if not target_id:
                continue
            if relation == "depends_on":
                blocker_id = target_id
                blocked_id = ticket_id
            else:  # "blocks"
                blocker_id = ticket_id
                blocked_id = target_id
            blocked_by.setdefault(blocked_id, set()).add(blocker_id)

    def all_blockers_closed(ticket_id: str) -> bool:
        blockers = blocked_by.get(ticket_id, set())
        for blocker_id in blockers:
            blocker_state = ticket_states.get(blocker_id)
            if blocker_state is None:
                # Tombstoned / missing → treat as closed
                continue
            if not _is_closed(blocker_state.get("status", "open")):
                return False
        return True

    ready: list[dict] = []
    for ticket_id, state in ticket_states.items():
        status = state.get("status", "open")
        if not _is_open(status):
            continue
        if epic_filter is not None and state.get("parent_id") != epic_filter:
            continue
        if all_blockers_closed(ticket_id):
            ready.append(state)

    return ready


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _get_tracker_dir() -> str:
    """Resolve the tracker directory from env or git root."""
    env_dir = os.environ.get("TICKETS_TRACKER_DIR")
    if env_dir:
        return env_dir

    # Derive from git root
    import subprocess  # noqa: PLC0415

    try:
        repo_root = (
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
        return os.path.join(repo_root, ".tickets-tracker")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(
            "Error: TICKETS_TRACKER_DIR not set and not inside a git repository",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="List tickets that are ready to work on.",
    )
    parser.add_argument(
        "--format",
        default="ids",
        choices=["ids", "llm"],
        help="Output format: 'ids' (one ID per line, default) or 'llm' (JSONL).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a single JSON array of compiled ticket-state dicts (same "
            "element shape as `list`/`search`). Takes precedence over --format."
        ),
    )
    parser.add_argument(
        "--epic",
        default=None,
        metavar="EPIC_ID",
        help="Scope output to direct children of this epic.",
    )

    args = parser.parse_args()

    tracker_dir = _get_tracker_dir()

    epic_filter = args.epic
    if epic_filter:
        _scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from ticket_resolver import resolve_ticket_id

        # find_ready_tickets returns an empty result when no children match
        # the filter (graceful); preserve that for unknown epics by falling
        # through to the raw input rather than failing loud here.
        epic_filter = resolve_ticket_id(epic_filter, tracker_dir) or epic_filter

    ready = find_ready_tickets(tracker_dir, epic_filter=epic_filter)

    # --json wins over --format: emit ONE JSON array of compiled ticket-state
    # dicts. `find_ready_tickets` already returns these state dicts straight
    # from the same reducer the --format=llm branch consumes, so this reuses the
    # identical data path while matching the array shape `list`/`search` emit.
    if args.json:
        print(json.dumps(ready, ensure_ascii=False))
        return 0

    if args.format == "llm":
        from ticket_reducer.llm_format import to_llm  # noqa: PLC0415

        for state in ready:
            print(json.dumps(to_llm(state), ensure_ascii=False))
    else:
        for state in ready:
            tid = state.get("ticket_id")
            if tid:
                print(tid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
