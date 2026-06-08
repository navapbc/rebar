#!/usr/bin/env python3
"""Ticket event reducer: compiles event files to current ticket state.

Reads all event JSON files in a ticket directory, sorts by filename
(lexicographic = chronological per the event format contract), and
folds them into a single state dict.

Usage:
    python3 ticket-reducer.py <ticket_dir_path>
    python3 ticket-reducer.py --batch <tracker_dir>

Module interface:
    from ticket_reducer import reduce_ticket  # package API
    state = reduce_ticket("/path/to/.tickets-tracker/tkt-001")
    all_states = reduce_all_tickets("/path/to/.tickets-tracker")
"""

from __future__ import annotations

import json
import os
import sys

# Ensure the ticket_reducer subpackage (sibling directory) is importable
# regardless of how this script is invoked (direct exec, importlib, etc.).
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ticket_reducer import (  # noqa: E402
    make_error_dict,
    reduce_all_tickets,
    reduce_ticket,
)
from ticket_reducer._api import (  # noqa: E402 — re-exports for backward compat
    LastTimestampWinsStrategy,  # noqa: F401
    MostStatusEventsWinsStrategy,  # noqa: F401
    ReducerStrategy,  # noqa: F401
    _is_net_archived,  # noqa: F401
)


def _make_error_dict(ticket_id: str, status: str, error: str) -> dict:
    """Build an error-state dict with all standard schema fields (d145-e1a9).

    Thin wrapper — delegates to ticket_reducer.make_error_dict so tests that
    import this symbol directly from this module continue to work.
    """
    return make_error_dict(ticket_id, status, error)


def main() -> int:
    """CLI entry point: print compiled ticket state as JSON."""
    args = sys.argv[1:]
    exclude_archived = False
    if "--exclude-archived" in args:
        exclude_archived = True
        args = [a for a in args if a != "--exclude-archived"]

    if len(args) == 2 and args[0] == "--batch":
        batch_dir = args[1]
        if not os.path.isdir(batch_dir):
            print(f"Error: directory not found: {batch_dir}", file=sys.stderr)
            return 1
        results = reduce_all_tickets(batch_dir, exclude_archived=exclude_archived)
        print(json.dumps(results, ensure_ascii=False))
        return 0

    if len(args) != 1:
        print("Usage: ticket-reducer.py <ticket_dir_path>", file=sys.stderr)
        print(
            "       ticket-reducer.py --batch [--exclude-archived] <tracker_dir>",
            file=sys.stderr,
        )
        return 1

    ticket_dir = args[0]

    if not os.path.isdir(ticket_dir):
        print(f"Error: directory not found: {ticket_dir}", file=sys.stderr)
        return 1

    state = reduce_ticket(ticket_dir)

    if state is None:
        print(f"Error: no CREATE event found in {ticket_dir}", file=sys.stderr)
        return 1

    if state.get("status") in ("error", "fsck_needed"):
        print(json.dumps(state, ensure_ascii=False))
        print(
            f"Error: ticket in {ticket_dir} has status '{state['status']}': "
            f"{state.get('error', 'unknown')}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
