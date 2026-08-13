"""Pre-lock preparation of a ticket event: validate, canonicalise, stage.

The first of the three concerns the store's write path is split into. This one runs
BEFORE any lock is taken and touches no git: it checks the event dict against the I2
write allow-list, serialises it to the CANONICAL committed bytes, and hands those bytes
to the staging layer. ``_store/event_append.py`` then takes the write lock and publishes
what this module prepared; ``_store/event_commit_git.py`` owns the git verbs it issues.

**Byte parity (the contract).** The committed bytes come from the single canonical
serializer :func:`rebar._store.canonical.canonical_bytes`
(``json.dumps(event, ensure_ascii=False, separators=(',', ':'), sort_keys=True)`` with
NO trailing newline), shared by every live event writer and pinned Python-to-Python by
``tests/interfaces/store/test_canonical_event_bytes.py`` (and the byte contract +
structural guard in ``tests/unit/test_canonical.py``). This module serialises the *given*
dict; it never re-derives author/env_id/uuid/timestamp (those are the seam's).

The atomic-publish convention itself — the scanner-invisible ``.tmp-newticket`` staging
directory and its single-rename promote — belongs to ``_store/staging.py`` and is
documented there; :func:`_prepare_event` composes with it rather than restating it.
"""

from __future__ import annotations

import os
from typing import Any

from rebar._store import staging as _staging
from rebar._store.canonical import canonical_bytes  # the single canonical serializer
from rebar.reducer._version import (  # single source of truth for the type names
    KEY_ADD,
    KEY_REVOKE,
    TAG_DELTA,
)

# I2 event-type enum (matches write_commit_event's `case` allow-list).
EVENT_TYPES = frozenset(
    {
        "CREATE",
        "STATUS",
        "COMMENT",
        "LINK",
        "UNLINK",
        "SNAPSHOT",
        "SYNC",
        "REVERT",
        "EDIT",
        "ARCHIVED",
        "FILE_IMPACT",
        "VERIFY_COMMANDS",
        "SIGNATURE",
        # Workflow run-state (epic a88f / WS-C1): a run + its per-step records.
        "WORKFLOW_RUN",
        "WORKFLOW_STEP",
        # Commits-on-ticket (epic a88f / WS-H).
        "COMMITS",
        # Tag add/remove deltas (epic P2.3).
        TAG_DELTA,
        # Identity key lifecycle (epic gnu-whale-ichor / e165): signed add/revoke.
        KEY_ADD,
        KEY_REVOKE,
        # Plan-review observability sidecar (epic 5fd2 / child db7b). Reducer-IGNORED
        # (NOT in KNOWN_EVENT_TYPES) so it never enters compiled state / hot paths and
        # compaction preserves it; it is in this WRITE allow-list so it can be emitted.
        "REVIEW_RESULT",
        # Completion-verifier FAIL observability sidecar (ticket 24ec). Reducer-IGNORED
        # (like REVIEW_RESULT) so it never enters compiled state / hot paths and compaction
        # preserves it; in this WRITE allow-list so it can be emitted, and in
        # _NON_REPLAY_KNOWN_TYPES so fsck recognises it and does not warn.
        "COMPLETION_VERDICT",
        # Cross-ticket overlap detection digest sidecar (epic only-crave-art / 2d0f).
        # Reducer-IGNORED (like REVIEW_RESULT) — a content-hash-keyed per-ticket Cupid
        # digest; in this WRITE allow-list so it can be emitted, in _NON_REPLAY_KNOWN_TYPES
        # so fsck recognises it and does not warn.
        "TICKET_DIGEST",
        # Enrichment queue sidecar events (epic only-crave-art / e1f4): cert-triggered
        # enqueue with a soak deadline, optimistic claim + lease, and done tombstone.
        # Reducer-IGNORED (like REVIEW_RESULT/TICKET_DIGEST) — a broker-less queue on the
        # event store; the drain reduces them out-of-band.
        "ENQUEUE_ENRICH",
        "CLAIM_ENRICH",
        "DONE_ENRICH",
    }
)


class StoreError(Exception):
    """A write-path failure carrying the bash-parity ``returncode`` + stderr text."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        self.returncode = returncode
        super().__init__(message)


def event_filename(timestamp: int, uuid_str: str, event_type: str) -> str:
    """The I2 filename: ``{timestamp}-{uuid}-{TYPE}.json``."""
    return f"{timestamp}-{uuid_str}-{event_type}.json"


def _ensure_initialized(tracker: str) -> None:
    """Raise :class:`StoreError` (1) if *tracker* is not an initialized store."""
    if not os.path.isdir(tracker) or not os.path.exists(os.path.join(tracker, ".git")):
        raise StoreError("Error: ticket system not initialized. Run 'ticket init' first.", 1)


def _validate_event(event: dict[str, Any]) -> tuple[str, Any, Any]:
    """Return ``(event_type, timestamp, uuid_str)`` for a well-formed event, else
    raise :class:`StoreError` (1) with the exact bash stderr. No disk/lock effect."""
    event_type = str(event.get("event_type", "")).upper()
    timestamp, uuid_str = event.get("timestamp"), event.get("uuid")
    if not event_type or timestamp is None or not uuid_str:
        raise StoreError(
            "Error: event JSON missing required fields (event_type, timestamp, uuid)", 1
        )
    if event_type not in EVENT_TYPES:
        raise StoreError(
            f"Error: invalid event_type '{event_type}'. Must be one of: CREATE, STATUS, "
            "COMMENT, LINK, UNLINK, SNAPSHOT, SYNC, REVERT, EDIT, ARCHIVED, FILE_IMPACT, "
            f"VERIFY_COMMANDS, SIGNATURE, WORKFLOW_RUN, WORKFLOW_STEP, COMMITS, {TAG_DELTA}, "
            f"{KEY_ADD}, {KEY_REVOKE}, "
            "REVIEW_RESULT, COMPLETION_VERDICT, TICKET_DIGEST, ENQUEUE_ENRICH, CLAIM_ENRICH, "
            "DONE_ENRICH",
            1,
        )
    return event_type, timestamp, uuid_str


def _prepare_event(
    tracker: str,
    ticket_id: str,
    event: dict[str, Any],
    *,
    sweep_stale: bool = True,
) -> _staging.StagedEvent:
    """Validate the event and stage its CANONICAL bytes for an atomic publish.

    Ticket 021d: a NEW ticket's directory is built inside a scanner-invisible staging path
    and published by ONE rename at :meth:`StagedEvent.promote`, so the directory and its
    first event become visible together and an interruption can no longer strand an empty
    ticket directory (the ``MISSING_CREATE`` + ``FOREIGN_STORE_PATH`` debris signature).
    This supersedes bug 043f's "the directory is created before its first event" decision
    at the WRITER only; 043f's actual ruling — that a reader tolerates an event-less
    directory and never tidies one — is untouched, and still repairs clones that already
    carry debris, which no writer-side change can do. See ``_store/staging.py``.

    Returns the staged event; raises :class:`StoreError` (1). No lock is held here."""
    event_type, timestamp, uuid_str = _validate_event(event)
    filename = event_filename(timestamp, uuid_str, event_type)
    try:
        return _staging.stage_event(
            tracker,
            ticket_id,
            filename,
            canonical_bytes(event),
            sweep_stale=sweep_stale,
        )
    except OSError as exc:
        raise StoreError("Error: failed to write staging temp file", 1) from exc
