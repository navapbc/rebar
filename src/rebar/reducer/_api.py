"""Public reduce_ticket / reduce_all_tickets API for the ticket_reducer package.

Exposes the two primary entry points so callers can do:

    from . import reduce_ticket, reduce_all_tickets

without spawning a ticket-reducer.py subprocess.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from ._cache import is_active_event, prepare_event_files, write_cache
from ._replay import replay_events
from ._state import make_error_dict, make_initial_state
from .marker import check_marker, remove_marker, write_marker

# The NON-GRAPH ARTIFACT ticket types — verbose/bulk bodies (session_log, code_review) kept OUT
# of the dependency-graph / store-health hot paths and default `list` (searchable via search/show).
# The `exclude_session_logs` reducer flag drops exactly these (the flag name predates code_review).
_NON_GRAPH_ARTIFACT_TYPES = ("session_log", "code_review", "identity")


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


def _heal_missing_marker(ticket_dir: str, state: dict) -> None:
    """Re-materialize a missing ``.archived`` fast-path marker for a net-archived
    ticket (self-healing cache; mirror of the stale-marker clearing in
    :func:`reduce_all_tickets`). A peer merging the untrack-runtime-markers ensure
    commit has git delete its worktree marker copies — safe, because archival's
    source of truth is the ARCHIVED events already reflected in compiled state —
    so the next list restores the fast path here."""
    if state.get("archived") and not check_marker(ticket_dir):
        write_marker(ticket_dir)


def _compute_preconditions_summary(ticket_dir: str) -> dict:
    """Scan ticket_dir for PRECONDITIONS events and compute LWW-merged summary.

    Transparently handles both flat event format and compacted snapshot format.
    Excludes *.retired files. Returns {"status": "pre-manifest"} when no events exist.
    """
    # Check for compacted snapshot first
    snapshot_files = sorted(
        f
        for f in os.listdir(ticket_dir)
        if f.endswith("-PRECONDITIONS-SNAPSHOT.json") and is_active_event(f)
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
            and is_active_event(f)
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
    *,
    include_retired: bool = False,
    event_files_override: list[str] | None = None,
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

    ``include_retired=True`` (RC2b Option 1, the fsck rebuild path) folds the
    append-only ``*.retired`` sources back in, strips SNAPSHOTs, and replays the
    full raw history from scratch — reconstructing state a stale snapshot's
    positional skip had silently dropped. It bypasses the reducer cache.

    ``event_files_override`` reduces an explicit, pre-sorted subset of event file
    paths instead of scanning the directory (compaction uses this to compute the
    SNAPSHOT over the *foldable* subset only). The result still gets the normal
    post-replay treatment (preconditions summary, ``updated_at``) so a
    subset-derived SNAPSHOT stays byte-parity with a full reduce when the subset is
    the whole set. This path is never cached.
    """
    ticket_dir = os.path.normpath(str(ticket_dir_path))
    ticket_id = os.path.basename(ticket_dir)

    if event_files_override is not None:
        cache_path = os.path.join(ticket_dir, ".cache.json")
        dir_hash = ""  # subset reduce — never keyed to (or served from) the cache
        event_files = event_files_override
    else:
        cache_path, dir_hash, event_files, cached = prepare_event_files(
            ticket_dir, include_retired=include_retired
        )
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
        state,
        event_files,
        ticket_id,
        cache_path,
        dir_hash,
        tracker_dir=tracker_dir,
        include_retired=include_retired,
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
        # The subset (override) and rebuild (include_retired) paths are never keyed to
        # the active-only dir hash, so they must not write the reducer cache.
        if event_files_override is None and not include_retired:
            write_cache(cache_path, dir_hash, result, ticket_dir)

    return result


def _excluded(
    state: dict,
    *,
    exclude_archived: bool,
    exclude_deleted: bool,
    exclude_session_logs: bool,
) -> bool:
    """True when the ``reduce_all_tickets`` exclusion flags drop ``state``.

    Applied per ticket inside the loop (bug humdrum-priestly-lcont) so a dropped ticket is
    never retained. Error dicts carry ``status`` ``"error"`` and no ``ticket_type``, so
    only ``exclude_archived`` could ever drop one — exactly the old post-loop filters.
    """
    if exclude_archived and state.get("archived"):
        return True
    if exclude_deleted and state.get("status") == "deleted":
        return True
    return exclude_session_logs and state.get("ticket_type") in _NON_GRAPH_ARTIFACT_TYPES


def _project(
    state: dict, omit_fields: tuple[str, ...], keep_fields: tuple[str, ...] | None
) -> dict:
    """Return ``state`` without ``omit_fields`` and, when given, only ``keep_fields``."""
    if omit_fields:
        state = {k: v for k, v in state.items() if k not in omit_fields}
    if keep_fields is not None:
        state = {k: v for k, v in state.items() if k in keep_fields}
    return state


def _reduce_entry(entry_path: str, *, exclude_archived: bool) -> tuple[dict | None, bool]:
    """Reduce one store directory for ``reduce_all_tickets``.

    Returns ``(state, reduced)``. ``state`` is None when a net-archived directory with an
    ``.archived`` marker is skipped without a reduce (``exclude_archived``; a stale marker
    is cleared). A failed reduce yields a ``reducer_failed`` error dict with ``reduced``
    False, so it is never projected.
    """
    if exclude_archived and os.path.exists(os.path.join(entry_path, ".archived")):
        if _is_net_archived(entry_path):
            return None, False
        remove_marker(entry_path)
    state = reduce_ticket(entry_path)
    if state is None:
        return make_error_dict(os.path.basename(entry_path), "error", "reducer_failed"), False
    if exclude_archived:
        _heal_missing_marker(entry_path, state)
    return state, True


def reduce_all_tickets(
    tracker_dir: str | os.PathLike[str],
    *,
    exclude_archived: bool = False,
    exclude_deleted: bool = False,
    exclude_session_logs: bool = False,
    omit_fields: tuple[str, ...] = (),
    keep: Callable[[dict], bool] | None = None,
    keep_fields: tuple[str, ...] | None = None,
) -> list[dict]:
    """Batch-reduce all tickets in tracker_dir.

    Every exclusion and ``keep`` is applied to each ticket *inside* the loop, so a
    ticket the caller will discard is never retained while the rest of the store is
    reduced (bug humdrum-priestly-lcont: up to 349 MB peak on a 9,700-ticket store).

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
            not an event re-scan; error dicts (status ``"error"``) are kept.
        exclude_session_logs: When True, drop the NON-GRAPH ARTIFACT types —
            ``session_log`` AND ``code_review`` (see :data:`_NON_GRAPH_ARTIFACT_TYPES`).
            Default on for the dependency-graph / store-health hot paths (``ready``,
            ``next_batch``, ``deps``, ``validate``) so verbose log/artifact bodies never
            tax those compiles; keeps them out of default ``list``. ``search`` and
            single-ticket ``show`` deliberately do NOT set this (they stay
            discoverable). Error dicts (no ``ticket_type``) are kept intact.
        omit_fields: Field names dropped from EACH compiled state *inside* the
            per-ticket loop, before it is retained. Default ``()`` — every
            existing caller keeps byte-identical results. This is a PEAK-MEMORY
            seam, not a wire-format one: a projection applied after this function
            returns cannot help, because by then the full state of every ticket in
            the store is simultaneously live (measured on the real store: 243.4 MB
            for 8,879 tickets, versus 56.4 MB projecting inline). The projection is
            pure — it builds a new dict and never mutates the reducer's cached
            state. A ``reducer_failed`` error dict is never projected.
        keep: Optional per-ticket predicate, called on the full state (error dicts
            included) after the exclusions; a ticket it rejects is dropped at once.
            The same peak-memory seam as ``omit_fields`` for callers that filter.
        keep_fields: Optional whitelist projection, the counterpart of
            ``omit_fields`` (both may be given). Applied after ``keep``.
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
        state, reduced = _reduce_entry(entry_path, exclude_archived=exclude_archived)
        if state is None or _excluded(
            state,
            exclude_archived=exclude_archived,
            exclude_deleted=exclude_deleted,
            exclude_session_logs=exclude_session_logs,
        ):
            continue
        if keep is not None and not keep(state):
            continue
        if reduced:
            state = _project(state, omit_fields, keep_fields)
        results.append(state)

    return results
