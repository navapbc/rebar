"""Health record module for the DSO reconciler.

Writes structured JSON health records after each reconciler pass so operators
can track fsck totals, per-type open counts, and mutation volume over time.
"""

from __future__ import annotations

import os

import json
import time
from pathlib import Path

SCHEMA_VERSION = 1

# Canonical state-directory layout for the dso reconciler. The two-level
# bridge_state/<feature> structure is part of the documented bridge contract
# (see the bridge README). Consuming projects override the *location* by
# passing a different repo_root; the layout itself is fixed.
_STATE_SUBDIR = "bridge_state"
_HEALTH_SUBDIR = "health"
_TICKETS_TRACKER_SUBDIR = ".tickets-tracker"


def record_pass(
    pass_id: str,
    pre_fsck: int,
    post_fsck: int,
    per_type_counts: dict,
    local_mutation_count: int,
    repo_root: Path | None = None,
    failure_kind: str | None = None,
) -> Path:
    """Write a health record for a completed reconciler pass.

    Args:
        pass_id: Unique identifier for this reconciler pass.
        pre_fsck: Bridge fsck total before the pass.
        post_fsck: Bridge fsck total after the pass.
        per_type_counts: Open count per ticket type (e.g. {epic, story, task, bug}).
        local_mutation_count: Number of mutations applied during this pass.
        repo_root: Repository root path. Defaults to four levels above this
            file (resolved at runtime via ``Path(__file__).parents[4]``).
        failure_kind: Optional indicator that the pass failed (e.g.
            ``"apply_error"``, ``"reschedule"``). When set, the record carries
            a ``failure_kind`` field so monitoring can distinguish degraded
            passes from successful zero-mutation passes. F8.

    Returns:
        Path to the written JSON health record file.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])  # four levels up from dso_reconciler/
    health_dir = repo_root / _STATE_SUBDIR / _HEALTH_SUBDIR
    health_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": SCHEMA_VERSION,
        "pass_id": pass_id,
        "pre_pass_fsck_total": pre_fsck,
        "post_pass_fsck_total": post_fsck,
        "per_type_open_counts": per_type_counts,
        "local_mutation_count_at_pass": local_mutation_count,
        "timestamp_ns": time.time_ns(),
    }
    if failure_kind is not None:
        record["failure_kind"] = failure_kind
    out_path = health_dir / f"{pass_id}.json"
    out_path.write_text(json.dumps(record, indent=2))
    return out_path


def count_open_by_type(repo_root: Path | None = None) -> dict:
    """Count open local tickets by type from the ticket tracker directory.

    Walks the ticket store, reads CREATE (type) and the latest STATUS event
    for each ticket directory, and returns ``{type: count}`` for open tickets.

    File-order contract (load-bearing):
        Event filenames MUST be timestamp-prefixed (e.g. ``{ts_ns}-create.json``,
        ``{ts_ns}-status.json``) so that ``sorted(ticket_dir.glob("*.json"))``
        yields the events in chronological order. The "latest STATUS wins"
        logic depends on this — if a ticket has multiple STATUS events
        (open -> closed -> open), the final iteration sets ``latest_status``,
        so iteration order must equal write order for the result to be
        correct. This is the canonical event-filename convention emitted by
        ``ticket-create.sh`` / ``ticket-transition.sh``.

    Args:
        repo_root: Repository root path. Defaults to four levels above this
            file.

    Returns:
        Dict mapping ticket type string to open-ticket count.  Only types with
        at least one open ticket are included.  Returns ``{}`` when the tracker
        directory is absent.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])  # repo root from dso_reconciler/

    tickets_dir = repo_root / _TICKETS_TRACKER_SUBDIR  # tickets-boundary-ok: direct event read for perf
    counts: dict[str, int] = {}
    if not tickets_dir.is_dir():
        return counts

    for ticket_dir in tickets_dir.iterdir():
        if not ticket_dir.is_dir():
            continue
        # Skip .scratch/ — it is a scratch-space for agent scratch data, not a
        # ticket directory.  Including it would cause key errors when the health
        # reader tries to parse scratch JSON envelopes as ticket events.
        if '.scratch' in ticket_dir.parts:
            continue
        event_files = sorted(ticket_dir.glob("*.json"))
        ticket_type: str | None = None
        # Default to "open" so tickets with only a CREATE event (no explicit
        # STATUS transition yet) match the canonical reducer initial state
        # (ticket_reducer/_state.py:make_initial_state in the dso scripts).
        latest_status: str = "open"
        for ef in event_files:
            try:
                event = json.loads(ef.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            # Real events nest payload under "data"; read from there, not the
            # top-level event dict (canonical shape per ticket-create.sh /
            # ticket-transition.sh / ticket_reducer/_processors.py).
            data = event.get("data", {}) if isinstance(event, dict) else {}
            evt = event.get("event_type", "") if isinstance(event, dict) else ""
            if evt == "CREATE":
                ticket_type = data.get("ticket_type", "") or ""
            elif evt == "STATUS":
                latest_status = data.get("status", "") or ""
        if ticket_type and latest_status == "open":
            counts[ticket_type] = counts.get(ticket_type, 0) + 1

    return counts


def capture_baseline(pass_id: str, repo_root: Path | None = None) -> Path:
    """Capture pre-pass fsck total from the current ticket store state.

    Reads the current open ticket count from the ticket store by inspecting
    the latest STATUS event for each ticket directory. Stores the result as a
    baseline snapshot so the reconciler can compare post-pass totals.

    File-order contract (load-bearing):
        Event filenames MUST be timestamp-prefixed (e.g. ``{ts_ns}-create.json``,
        ``{ts_ns}-status.json``) so that ``sorted(ticket_dir.glob("*.json"))``
        yields the events in chronological order. The "latest STATUS wins"
        logic depends on this — if a ticket has multiple STATUS events
        (open -> closed -> open), the final iteration sets ``latest_status``,
        so iteration order must equal write order for the result to be
        correct. The has_create + default-open pattern is event-order
        independent: any CREATE anywhere in the directory marks the ticket
        as present, and the absence of any STATUS leaves it at the canonical
        reducer initial state of "open".

    Args:
        pass_id: Unique identifier for this reconciler pass.
        repo_root: Repository root path. Defaults to four levels above this
            file (resolved at runtime via ``Path(__file__).parents[4]``).

    Returns:
        Path to the written JSON baseline file.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])  # repo root from dso_reconciler/

    # Count total open tickets across all types as the baseline fsck total.
    tickets_dir = repo_root / _TICKETS_TRACKER_SUBDIR  # tickets-boundary-ok
    total_open = 0
    if tickets_dir.is_dir():
        for ticket_dir in tickets_dir.iterdir():
            if not ticket_dir.is_dir():
                continue
            # Skip .scratch/ — scratch-space entries are not ticket directories.
            if '.scratch' in ticket_dir.parts:
                continue
            # Walk all events. Tickets with only a CREATE (no STATUS yet)
            # default to "open" to match the canonical reducer initial state
            # (ticket_reducer/_state.py:make_initial_state); real STATUS payloads
            # live under event["data"]["status"], not the top-level event dict.
            event_files = sorted(ticket_dir.glob("*.json"))
            has_create = False
            latest_status: str = ""
            for ef in event_files:
                try:
                    event = json.loads(ef.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(event, dict):
                    continue
                evt = event.get("event_type", "")
                data = event.get("data", {})
                if evt == "CREATE":
                    has_create = True
                elif evt == "STATUS":
                    latest_status = data.get("status", "") or ""
            if has_create and not latest_status:
                latest_status = "open"
            if latest_status == "open":
                total_open += 1

    health_dir = repo_root / _STATE_SUBDIR / _HEALTH_SUBDIR
    health_dir.mkdir(parents=True, exist_ok=True)
    baseline = {
        "pass_id": pass_id,
        "pre_pass_fsck_total": total_open,
        "timestamp_ns": time.time_ns(),
    }
    out_path = health_dir / f"{pass_id}_baseline.json"
    out_path.write_text(json.dumps(baseline, indent=2))
    return out_path
