#!/usr/bin/env python3
"""ticket-search.py — full-text search over tickets (WS5a).

REPLAY-DERIVED query: reduces every ticket from the append-only event log via
``reduce_all_tickets`` and matches the query in-process against title,
description, tags, and comment bodies. Optional --status / --type / --has-tag
filters. Outputs a JSON array of matching ticket states (same element shape as
``ticket list``).

Concurrency (REMEDIATION_PROPOSAL §0 / docs/concurrency.md): this is a pure read
— it computes results from replay on demand and writes NO committed index (an
index would violate I6). The reducer's local, gitignored ``.cache.json``
(I3/I3a) is the only read-side cache. So search adds no new concurrency surface.

Usage:
  ticket-search.py <query> [--status=S] [--type=T] [--has-tag=TAG]
                           [--include-archived]
Exit: 0 (always, on a valid query; emits [] when nothing matches).
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


def _tracker_dir() -> str:
    env_dir = os.environ.get("TICKETS_TRACKER_DIR")
    if env_dir:
        return env_dir
    root = os.environ.get("PROJECT_ROOT") or os.environ.get("REBAR_ROOT")
    if root:
        return os.path.join(root, ".tickets-tracker")
    import subprocess
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL
        ).decode().strip()
        return os.path.join(repo_root, ".tickets-tracker")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: not inside a git repository (set REBAR_ROOT)", file=sys.stderr)
        sys.exit(1)


def _haystack(state: dict) -> str:
    parts = [
        str(state.get("title") or ""),
        str(state.get("description") or ""),
        " ".join(str(t) for t in (state.get("tags") or [])),
    ]
    for c in state.get("comments") or []:
        if isinstance(c, dict):
            parts.append(str(c.get("body") or ""))
        else:
            parts.append(str(c))
    return "\n".join(parts).lower()


def search(states: list[dict], query: str, *, status=None, ticket_type=None,
           has_tag=None) -> list[dict]:
    # AND over whitespace-split, case-insensitive terms.
    terms = [t for t in query.lower().split() if t]
    out = []
    for st in states:
        if not isinstance(st, dict) or "status" not in st:
            continue  # skip error dicts
        if status is not None and st.get("status") != status:
            continue
        if ticket_type is not None and st.get("ticket_type") != ticket_type:
            continue
        if has_tag is not None and has_tag not in (st.get("tags") or []):
            continue
        hay = _haystack(st)
        if all(term in hay for term in terms):
            out.append(st)
    return out


def main(argv) -> int:
    query = None
    status = ticket_type = has_tag = None
    include_archived = False
    for arg in argv[1:]:
        if arg.startswith("--status="):
            status = arg[len("--status="):]
        elif arg.startswith("--type="):
            ticket_type = arg[len("--type="):]
        elif arg.startswith("--has-tag="):
            has_tag = arg[len("--has-tag="):]
        elif arg == "--include-archived":
            include_archived = True
        elif arg.startswith("--"):
            continue  # tolerate unknown flags
        elif query is None:
            query = arg
    if query is None:
        print("Usage: ticket search <query> [--status=S] [--type=T] [--has-tag=TAG]", file=sys.stderr)
        return 2

    states = reduce_all_tickets(
        _tracker_dir(),
        exclude_archived=not include_archived,
        exclude_deleted=True,
    )
    results = search(states, query, status=status, ticket_type=ticket_type, has_tag=has_tag)
    from ticket_reducer._present import public_state
    print(json.dumps([public_state(t) for t in results], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
