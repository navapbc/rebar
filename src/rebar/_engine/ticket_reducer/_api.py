"""Public reduce_ticket / reduce_all_tickets API for the ticket_reducer package.

Exposes the two primary entry points so callers can do:

    from ticket_reducer import reduce_ticket, reduce_all_tickets

without spawning a ticket-reducer.py subprocess.
"""

from __future__ import annotations

import json
import os

from ticket_reducer._cache import prepare_event_files, write_cache
from ticket_reducer._processors import replay_events
from ticket_reducer._state import make_error_dict, make_initial_state
from ticket_reducer.marker import remove_marker


def _is_net_archived(ticket_dir: str) -> bool:
    """Return True only if the ticket's net archival state is archived."""
    archived_uuids: set[str] = set()
    reverted_uuids: set[str] = set()
    for fname in os.listdir(ticket_dir):
        if fname.startswith(".") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(ticket_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        etype = event.get("event_type", "")
        uuid = event.get("uuid", "")
        if etype == "ARCHIVED" and uuid:
            archived_uuids.add(uuid)
        elif etype == "REVERT":
            target = event.get("data", {}).get("target_event_uuid", "")
            if target:
                reverted_uuids.add(target)
    return bool(archived_uuids - reverted_uuids)


def _compute_preconditions_summary(ticket_dir: str) -> dict:
    """Scan ticket_dir for PRECONDITIONS events and compute LWW-merged summary.

    Transparently handles both flat event format and compacted snapshot format.
    Excludes *.retired files. Returns {"status": "pre-manifest"} when no events exist.
    """
    # Check for compacted snapshot first
    snapshot_files = sorted(
        f
        for f in os.listdir(ticket_dir)
        if f.endswith("-PRECONDITIONS-SNAPSHOT.json") and not f.endswith(".retired")
    )
    if snapshot_files:
        snap_path = os.path.join(ticket_dir, snapshot_files[-1])
        try:
            with open(snap_path, encoding="utf-8") as fh:
                snap = json.load(fh)
            data = snap.get("data", snap)
            return {
                "status": "present",
                "manifest_depth": data.get("manifest_depth", 0),
                "gate_verdicts": data.get("gate_verdicts", {}),
                "source_count": data.get("source_count", 1),
                "compacted": True,
            }
        except (OSError, json.JSONDecodeError):
            pass  # fall through to flat event scan

    # Scan flat PRECONDITIONS event files (exclude snapshots and retired)
    event_files = sorted(
        f
        for f in os.listdir(ticket_dir)
        if (
            f.endswith("-PRECONDITIONS.json")
            and not f.endswith("-PRECONDITIONS-SNAPSHOT.json")
            and not f.endswith(".retired")
        )
    )
    if not event_files:
        return {"status": "pre-manifest"}

    # LWW merge: composite key = (gate_name, session_id, worktree_id)
    merged: dict[tuple[str, str, str], dict] = {}
    for fname in event_files:
        fpath = os.path.join(ticket_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                ev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        data = ev.get("data", {})
        key = (
            data.get("gate_name", ""),
            data.get("session_id", ""),
            data.get("worktree_id", ""),
        )
        ev_ts = ev.get("timestamp", 0)
        if key not in merged or ev_ts > merged[key]["_ts"]:
            merged[key] = dict(data)
            merged[key]["_ts"] = ev_ts

    gate_verdicts: dict[str, str] = {}
    manifest_depth = 0
    for payload in merged.values():
        gate_verdicts.update(payload.get("gate_verdicts", {}))
        d = payload.get("manifest_depth", 0)
        if d > manifest_depth:
            manifest_depth = d

    return {
        "status": "present",
        "manifest_depth": manifest_depth,
        "gate_verdicts": gate_verdicts,
        "source_count": len(event_files),
    }


def reduce_ticket(
    ticket_dir_path: str | os.PathLike[str],
) -> dict | None:
    """Compile all events in ticket_dir_path to current ticket state.

    Events are replayed in **filename order** (lexicographic = chronological per
    the event-format contract). Replay performs **UUID dedup**: among the events
    that actually replay, the *first* occurrence of a given event ``uuid`` (in
    filename order) is applied and any later file carrying the same ``uuid`` is
    skipped. This makes a duplicate event file (e.g. a COMMENT copied to a new
    filename with the same payload ``uuid``) apply exactly once. Dedup is scoped
    to replayed events only, so it composes with compaction: events before the
    latest SNAPSHOT — and events whose ``uuid`` the SNAPSHOT already captured —
    are skipped before dedup is consulted.
    """
    ticket_dir = os.path.normpath(str(ticket_dir_path))
    ticket_id = os.path.basename(ticket_dir)

    cache_path, dir_hash, event_files, cached = prepare_event_files(ticket_dir)
    if cached is not None:
        return cached
    if not event_files:
        return None

    # Derive tracker_dir so process_link can resolve alias-form target IDs to
    # canonical UUIDs.  ticket_dir is <tracker>/<ticket_id>, so the parent is
    # the tracker root.  When ticket_dir has no parent (unlikely but defensive),
    # we pass None and alias resolution degrades gracefully to verbatim storage.
    tracker_dir: str | None = os.path.dirname(ticket_dir) or None

    state = make_initial_state()
    valid_event_count, early_result = replay_events(
        state, event_files, ticket_id, cache_path, dir_hash, tracker_dir=tracker_dir
    )
    if early_result is not None:
        return early_result

    if state["ticket_type"] is None:
        result: dict | None = (
            make_error_dict(ticket_id, "error", "no_valid_create_event")
            if valid_event_count == 0 and len(event_files) > 0
            else None
        )
    else:
        result = state

    if result is not None:
        # Attach PRECONDITIONS summary (transparent to snapshot vs flat events)
        try:
            result["preconditions_summary"] = _compute_preconditions_summary(ticket_dir)
        except OSError:
            result["preconditions_summary"] = {"status": "pre-manifest"}
        write_cache(cache_path, dir_hash, result, ticket_dir)

    return result


def reduce_all_tickets(
    tracker_dir: str | os.PathLike[str],
    exclude_archived: bool = False,
    exclude_deleted: bool = False,
) -> list[dict]:
    """Batch-reduce all tickets in tracker_dir.

    Args:
        tracker_dir: Path to the ``.tickets-tracker`` directory.
        exclude_archived: When True, drop tickets whose net archival state is
            archived (and clear stale ``.archived`` markers). Default False.
        exclude_deleted: When True, drop tickets whose reduced ``status`` is
            ``"deleted"`` (terminal tombstones). This is independent of
            ``exclude_archived`` and defaults to False to preserve every
            existing caller — notably ``ticket list --include-archived``, which
            must still surface deleted tickets for tombstone inspection. Uses
            the reduced ``status`` field (the authoritative net-deleted signal),
            not an event re-scan; error dicts (no ``status``) are kept intact.
    """
    tracker_path = os.path.normpath(str(tracker_dir))
    results: list[dict] = []

    try:
        entries = sorted(os.listdir(tracker_path))
    except OSError:
        return results

    for entry in entries:
        if entry.startswith("."):
            continue
        entry_path = os.path.join(tracker_path, entry)
        if not os.path.isdir(entry_path):
            continue

        if exclude_archived and os.path.exists(os.path.join(entry_path, ".archived")):
            if _is_net_archived(entry_path):
                continue
            remove_marker(entry_path)

        state = reduce_ticket(entry_path)

        if state is None:
            results.append(make_error_dict(entry, "error", "reducer_failed"))
        else:
            results.append(state)

    if exclude_archived:
        results = [r for r in results if not r.get("archived")]

    if exclude_deleted:
        # Use the reduced status field as the authoritative net-deleted signal.
        # Error dicts have no "status" key and are preserved intact.
        results = [r for r in results if r.get("status") != "deleted"]

    return results
