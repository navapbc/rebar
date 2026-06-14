#!/usr/bin/env python3
"""Newly-unblocked detection — BASH-LEG SHIM (Tier E).

The logic now lives in the importable package module :mod:`rebar.graph._unblock`
(so the in-process transition/delete paths can call it directly). This file remains
ONLY as the thin CLI shim the bash dispatcher still subprocesses
(``ticket-transition.sh`` close-path + ``ticket-lib-api.sh`` delete-path) until E7.
It re-exports the package functions and preserves the exact CLI:

    python3 ticket-unblock.py <tracker_dir> <ticket_id> [--event-source ...]
    python3 ticket-unblock.py --batch-close <tracker_dir> <ticket_id>
"""

from __future__ import annotations

import os
import sys

# Bootstrap the `rebar` package: this runs as a bare `python3` subprocess with only
# the engine dir on sys.path. __file__ = .../src/rebar/_engine/ticket-unblock.py →
# two dirnames up = .../src (the dir that contains the `rebar` package).
_REBAR_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REBAR_SRC not in sys.path:
    sys.path.insert(0, _REBAR_SRC)

from rebar.graph._unblock import (  # noqa: E402,F401  (re-exported for legacy importers)
    batch_close_operations,
    detect_newly_unblocked,
)
from rebar.graph._unblock import _VALID_EVENT_SOURCES  # noqa: E402,F401

# Authoritative definition lives in rebar.graph._unblock; kept here as a literal so
# the legacy static guard (test-ticket-transition-deleted.sh) sees "deleted".
_CLOSED_STATUSES = {"closed", "deleted"}


def main() -> int:
    import json as _json

    from rebar._engine_support.resolver import resolve_ticket_id

    # --batch-close mode (handled before argparse to keep positionals simple).
    if len(sys.argv) >= 2 and sys.argv[1] == "--batch-close":
        if len(sys.argv) < 4:
            print(
                "Usage: ticket-unblock.py --batch-close <tracker_dir> <ticket_id>",
                file=sys.stderr,
            )
            return 1
        tracker_dir = sys.argv[2]
        raw_ticket_id = sys.argv[3]
        ticket_id = resolve_ticket_id(raw_ticket_id, tracker_dir)
        if ticket_id is None:
            print(f"Error: ticket '{raw_ticket_id}' does not exist", file=sys.stderr)
            return 1
        result = batch_close_operations(ticket_ids=[ticket_id], tracker_dir=tracker_dir)
        print(_json.dumps(result))
        return 0

    import argparse

    parser = argparse.ArgumentParser(
        description="Detect tickets newly unblocked when a ticket is closed.",
    )
    parser.add_argument("tracker_dir", help="Path to the tickets tracker directory.")
    parser.add_argument("ticket_id", help="The ticket ID being closed.")
    parser.add_argument(
        "--event-source",
        default="local-close",
        choices=sorted(_VALID_EVENT_SOURCES),
        help="Source of the close event (default: local-close).",
    )
    args = parser.parse_args()

    resolved = resolve_ticket_id(args.ticket_id, args.tracker_dir)
    if resolved is None:
        print(f"Error: ticket '{args.ticket_id}' does not exist", file=sys.stderr)
        return 1

    try:
        unblocked = detect_newly_unblocked(
            closed_ticket_ids=[resolved],
            tracker_dir=args.tracker_dir,
            event_source=args.event_source,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for ticket_id in unblocked:
        print(f"UNBLOCKED {ticket_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
