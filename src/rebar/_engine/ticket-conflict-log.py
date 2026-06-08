#!/usr/bin/env python3
"""Conflict resolution logger for ticket sync operations.

Appends a JSONL record to <tracker_dir>/conflict-resolutions.jsonl each time
a ticket status conflict is resolved deterministically by the
most-status-events-wins strategy.

Module interface (importlib required for hyphenated filename):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ticket_conflict_log", __file__
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_conflict_resolution(tracker_dir, ticket_id, env_ids, event_counts, winning_state)
"""

from __future__ import annotations

import json
import os
import sys
import time


def log_conflict_resolution(
    tracker_dir: str,
    ticket_id: str,
    env_ids: list[str],
    event_counts: dict[str, int],
    winning_state: str,
    bridge_env_excluded: bool = False,
) -> None:
    """Append one JSONL record documenting a conflict resolution decision.

    Parameters
    ----------
    tracker_dir:
        Path to the tracker directory.  The log file
        ``conflict-resolutions.jsonl`` is created inside this directory.
    ticket_id:
        The ticket whose status was in conflict.
    env_ids:
        Ordered list of environment IDs that participated in the conflict.
    event_counts:
        Mapping of env_id -> number of status events seen in that environment.
    winning_state:
        The resolved state selected by the strategy (most-status-events-wins).
    bridge_env_excluded:
        When True, indicates that the bridge environment was excluded from the
        event count comparison before resolution.  Defaults to False.

    Returns
    -------
    None
        Always returns None; write failures are non-fatal (logged to stderr).
    """
    record: dict = {
        "timestamp": time.time_ns(),
        "ticket_id": ticket_id,
        "env_ids": env_ids,
        "event_counts": event_counts,
        "winning_state": winning_state,
        "resolution_method": "most-status-events-wins",
        "bridge_env_excluded": bridge_env_excluded,
    }

    log_path = os.path.join(tracker_dir, "conflict-resolutions.jsonl")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
    except OSError as exc:
        print(
            f"WARNING: ticket-conflict-log: could not write to {log_path}: {exc}",
            file=sys.stderr,
        )
