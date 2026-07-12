"""The two-pass replay engine for the ticket reducer.

This is the *engine* half of the reducer (module-size seam, extracted from
:mod:`._processors`): given a ticket's event files it finds the latest SNAPSHOT
(pass 1) and folds the applicable events onto state by dispatching each through the
flat ``_EVENT_HANDLERS`` table over the ``process_*`` processors (pass 2). The
per-event fold logic itself lives in :mod:`._processors`; this module only owns the
scan / dispatch / snapshot-short-circuit machinery and depends on the processors
one-way (the processors never call back into the engine).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import NamedTuple

from ._processors import (
    process_archived,
    process_bridge_alert,
    process_comment,
    process_commits,
    process_create,
    process_edit,
    process_file_impact,
    process_key_event,
    process_link,
    process_revert,
    process_signature,
    process_snapshot,
    process_status,
    process_tag_delta,
    process_unlink,
    process_verify_commands,
    process_workflow_run,
    process_workflow_step,
)
from ._version import KEY_ADD, KEY_REVOKE, KNOWN_EVENT_TYPES, TAG_DELTA

logger = logging.getLogger(__name__)


def scan_for_latest_snapshot(
    event_files: list[str],
    *,
    include_retired: bool = False,
) -> tuple[int | None, set[str]]:
    """Pass 1: find the latest SNAPSHOT index and its source UUIDs.

    Returns (idx, source_uuids); idx None if no SNAPSHOT. Rebuild mode
    (``include_retired=True``) returns ``(None, set())`` — SNAPSHOTs pre-stripped.
    """
    if include_retired:
        return None, set()

    latest_snapshot_idx: int | None = None
    snapshot_source_uuids: set[str] = set()

    for idx, filepath in enumerate(event_files):
        try:
            with open(filepath, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if event.get("event_type") == "SNAPSHOT":
            latest_snapshot_idx = idx
            snapshot_source_uuids = set(event.get("data", {}).get("source_event_uuids", []))

    return latest_snapshot_idx, snapshot_source_uuids


class _ReplayCtx(NamedTuple):
    """The per-event arguments a replay handler may need. Bundled so the replay loop can
    dispatch through the single flat ``_EVENT_HANDLERS`` table below instead of a deep
    event-type if/elif tower — each handler pulls exactly the fields its processor takes."""

    state: dict
    event: dict
    data: dict
    ticket_id: str
    cache_path: str
    dir_hash: str
    filepath: str
    event_uuid: str
    tracker_dir: str | None


# Event-type -> per-event handler. Flattens the former deep if/elif dispatch tower in
# replay_events into an O(1) table over the existing ``process_*`` processors; each entry
# adapts the shared _ReplayCtx to its processor's signature. Only CREATE returns a value
# (the corrupt-CREATE early-return dict); every other processor returns None.
_EVENT_HANDLERS: dict[str, Callable[[_ReplayCtx], dict | None]] = {
    "CREATE": lambda c: process_create(
        c.state, c.event, c.data, c.ticket_id, c.cache_path, c.dir_hash
    ),
    "STATUS": lambda c: process_status(c.state, c.event, c.data, c.filepath),
    "COMMENT": lambda c: process_comment(c.state, c.event, c.data),
    "LINK": lambda c: process_link(c.state, c.event, c.data, tracker_dir=c.tracker_dir),
    "UNLINK": lambda c: process_unlink(c.state, c.data),
    "BRIDGE_ALERT": lambda c: process_bridge_alert(c.state, c.event, c.data, c.event_uuid),
    "REVERT": lambda c: process_revert(c.state, c.event, c.data, c.event_uuid),
    "EDIT": lambda c: process_edit(c.state, c.data),
    "FILE_IMPACT": lambda c: process_file_impact(c.state, c.event, c.data),
    "VERIFY_COMMANDS": lambda c: process_verify_commands(c.state, c.event, c.data),
    "SIGNATURE": lambda c: process_signature(c.state, c.event, c.data),
    "WORKFLOW_RUN": lambda c: process_workflow_run(c.state, c.event, c.data),
    "WORKFLOW_STEP": lambda c: process_workflow_step(c.state, c.event, c.data),
    "COMMITS": lambda c: process_commits(c.state, c.event, c.data),
    TAG_DELTA: lambda c: process_tag_delta(c.state, c.data),
    KEY_ADD: lambda c: process_key_event(c.state, c.event, c.data, KEY_ADD),
    KEY_REVOKE: lambda c: process_key_event(c.state, c.event, c.data, KEY_REVOKE),
    "ARCHIVED": lambda c: process_archived(c.state),
    "SNAPSHOT": lambda c: process_snapshot(c.state, c.data),
}


def replay_events(
    state: dict,
    event_files: list[str],
    ticket_id: str,
    cache_path: str,
    dir_hash: str,
    tracker_dir: str | None = None,
    *,
    include_retired: bool = False,
) -> tuple[int, dict | None]:
    """Pass 2: replay events onto state, applying each processor in order.

    Skips events before the latest SNAPSHOT index and events whose UUID appears in
    snapshot_source_uuids (already in the SNAPSHOT's compiled_state). Rebuild mode
    (``include_retired=True``) has no snapshot short-circuit — the full raw log
    (SNAPSHOTs pre-stripped) replays from index 0. Returns (valid_event_count,
    early_return_result); the latter is non-None only on a corrupt CREATE.
    """
    latest_snapshot_idx, snapshot_source_uuids = scan_for_latest_snapshot(
        event_files, include_retired=include_retired
    )
    start_idx = latest_snapshot_idx if latest_snapshot_idx is not None else 0
    valid_event_count = 0
    seen_uuids: set[str] = set()
    # Running max of event timestamps over the events that actually shape state
    # (post-snapshot/applied events + the SNAPSHOT's own compacted_at). Surfaced
    # as the derived ``updated_at`` (P1.1). Pre-snapshot events are skipped here
    # but their effect is folded into the SNAPSHOT's compacted_at, so the max
    # stays correct across compaction. Kept off the event log: compact.py strips
    # ``updated_at`` before serializing compiled_state (byte-parity guard).
    max_ts: int | None = None

    for idx, filepath in enumerate(event_files):
        try:
            with open(filepath, encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("skipping corrupt event %s", filepath, exc_info=True)
            continue

        valid_event_count += 1

        if idx < start_idx:
            continue

        event_uuid = event.get("uuid", "")
        if event_uuid and event_uuid in snapshot_source_uuids:
            continue

        # Dedup by UUID among events that actually replay: first occurrence in
        # filename order wins; a duplicate event file (same payload uuid under a
        # different filename) is skipped so it applies exactly once. Scoped to
        # post-snapshot, non-snapshot-source events, so zero interaction with
        # the compaction/snapshot machinery (bug 944c-374d).
        if event_uuid:
            if event_uuid in seen_uuids:
                continue
            seen_uuids.add(event_uuid)

        event_type = event.get("event_type", "")
        data = event.get("data", {})

        # Fold this applied event into the updated_at running max. A SNAPSHOT
        # re-seeds from its compacted_at (replay discards the pre-snapshot events
        # it summarizes), so a freshly-compacted, untouched ticket reports
        # updated_at == compacted_at.
        ev_ts = data.get("compacted_at") if event_type == "SNAPSHOT" else event.get("timestamp")
        if ev_ts is not None and (max_ts is None or ev_ts > max_ts):
            max_ts = ev_ts

        # KNOWN_EVENT_TYPES is the forward-compat authority, checked FIRST: an event kind
        # a NEWER rebar introduced — or, for a downgraded clone, a type absent from its
        # KNOWN_EVENT_TYPES — is preserved-and-ignored (skipped without error so the ticket
        # stays readable; ticket-compact keeps the file so an older clone's compaction can't
        # destroy a newer clone's data). Gating on KNOWN_EVENT_TYPES (not the static handler
        # table) is what lets a downgraded reducer that masks the type ignore it, matching
        # the pre-flattening if/elif tower's final `not in KNOWN_EVENT_TYPES` branch.
        if event_type not in KNOWN_EVENT_TYPES:
            pass
        else:
            # Authorship PRESENCE count (epic gnu-whale-ichor / 3183): tally every folded
            # (applied) event by whether its envelope carries an `author_sig`. This is
            # PRESENCE-ONLY — a bad/forged author_sig still folds normally (the reducer NEVER
            # rejects an event over authorship; cryptographic verification is the merge-gate's
            # job) and still counts as "signed" (present). SNAPSHOT is excluded: it re-seeds
            # the counts from its compiled_state (it summarizes already-counted events).
            if event_type != "SNAPSHOT":
                _bucket = "signed" if event.get("author_sig") else "unsigned"
                _counts = state.setdefault("authorship", {"signed": 0, "unsigned": 0})
                _counts[_bucket] = _counts.get(_bucket, 0) + 1
            handler = _EVENT_HANDLERS.get(event_type)
            if handler is not None:
                result = handler(
                    _ReplayCtx(
                        state,
                        event,
                        data,
                        ticket_id,
                        cache_path,
                        dir_hash,
                        filepath,
                        event_uuid,
                        tracker_dir,
                    )
                )
                # Only CREATE yields a value: the corrupt-CREATE early-return result.
                if result is not None:
                    return valid_event_count, result

    # Derived presentation field (not an event-log field). None when no applied
    # event carried a timestamp; the None-last sort key handles that.
    state["updated_at"] = max_ts
    return valid_event_count, None
