#!/usr/bin/env python3
"""Bulk scan all tickets to identify which are ready to work on.

A ticket is "ready" if:
  1. Its status is "open" or "in_progress"
  2. All direct blocker tickets (deps with relation "depends_on" or "blocks")
     are "closed" (or do not exist / tombstoned)

Usage:
    python3 ticket-ready.py [--output text|llm|json] [-o ...] [--epic=<epic_id>]

Environment:
    TICKETS_TRACKER_DIR — path to the tickets tracker directory.
    When absent, derived from `git rev-parse --show-toplevel`.

Output is selected by the canonical --output/-o flag (parsed by ticket_output):
    text (default):  one ticket ID per line
    llm:             one JSON object per line (JSONL), LLM-optimised format
    json:            a single JSON ARRAY of compiled ticket-state dicts (the
                     same element shape `list`/`search` emit, from the same
                     reducer data path).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ticket_graph._ready import find_ready_tickets  # noqa: E402
from ticket_output import OutputFormatError, parse_output  # noqa: E402

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

    # Resolve the canonical --output/-o flag via the single source of truth
    # (ticket_output), then let argparse handle the rest. 'text' (default) emits
    # one id per line; 'llm' emits JSONL; 'json' emits one array of states.
    try:
        out_format, rest = parse_output(sys.argv[1:], "ready")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        description="List tickets that are ready to work on.",
    )
    parser.add_argument(
        "--epic",
        default=None,
        metavar="EPIC_ID",
        help="Scope output to direct children of this epic.",
    )

    args = parser.parse_args(rest)

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

    # `find_ready_tickets` returns compiled ticket-state dicts straight from the
    # same reducer all output formats consume, so each branch reuses one data
    # path. public_state strips internal-only keys to match list/search exactly.
    from ticket_reducer._present import public_state  # noqa: PLC0415

    if out_format == "json":
        # ONE JSON array of compiled ticket-state dicts (list/search element shape).
        print(json.dumps([public_state(s) for s in ready], ensure_ascii=False))
    elif out_format == "llm":
        from ticket_reducer.llm_format import to_llm  # noqa: PLC0415

        for state in ready:
            print(json.dumps(to_llm(public_state(state)), ensure_ascii=False))
    else:  # text: one ticket id per line
        for state in ready:
            tid = state.get("ticket_id")
            if tid:
                print(tid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
