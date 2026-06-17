"""Canonical locked event-commit for the tickets store (Tier D, ``REBAR_WRITE_CORE``).

In-process replacement for the bash write path
``ticket-append-event.sh`` → ``write_commit_event`` → ``_flock_stage_commit``: takes
a fully-composed event dict (the seam already builds ``{timestamp, uuid,
event_type, env_id, author, data}``), serialises it to the CANONICAL committed
bytes, stages it same-filesystem, and under the unified write lock does the atomic
rename + ``git add`` + ``git commit``. ``write_and_push`` additionally runs the
best-effort push.

**Byte parity (the contract).** The committed bytes are
``json.dumps(event, ensure_ascii=False, separators=(',', ':'), sort_keys=True)`` with
NO trailing newline — byte-identical to the bash path's ``jq -S -c '.'`` (pinned by
``tests/scripts/test-ticket-write-commit-event.sh``). This committer serialises the
*given* dict; it never re-derives author/env_id/uuid/timestamp (those are the seam's).

Exit-code parity (surfaced as ``StoreError.returncode`` → the seam's ``CommandError``):
``1`` lock timeout / atomic-rename failure / git-commit failure (distinct stderr each),
``75`` rebase/merge guard. Mirrors ``_flock_stage_commit`` (which maps its internal
2/3 to an external return 1).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

from rebar._store import lock as _lock
from rebar._store.lock import LockTimeout, RebaseGuard  # re-export for callers

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


def _canonical_bytes(event: dict[str, Any]) -> bytes:
    """The committed bytes (== ``jq -S -c '.'``): sorted keys, compact separators,
    ``ensure_ascii=False``, no trailing newline."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _ensure_gc_auto_zero(tracker: str) -> None:
    """``gc.auto=0`` in the tickets worktree (skip if already 0), matching
    _flock_stage_commit's pre-lock guard. The value-check below already avoids the
    redundant ``git config`` write when it is set, so no separate skip flag."""
    cur = subprocess.run(
        ["git", "-C", tracker, "config", "--get", "gc.auto"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if cur != "0":
        subprocess.run(["git", "-C", tracker, "config", "gc.auto", "0"], check=False)


def stage_and_commit(tracker: str | os.PathLike, ticket_id: str, event: dict[str, Any]) -> int:
    """Validate, canonical-stage, lock, atomic-rename, ``git add``+``commit``.

    Returns 0 on success; raises :class:`StoreError` (1), :class:`RebaseGuard` (75),
    or :class:`LockTimeout` (1) with the exact bash stderr."""
    tracker = _lock.canonical_tracker(tracker)
    if not os.path.isdir(tracker) or not os.path.exists(os.path.join(tracker, ".git")):
        raise StoreError("Error: ticket system not initialized. Run 'ticket init' first.", 1)

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
            "VERIFY_COMMANDS, SIGNATURE",
            1,
        )

    ticket_dir = os.path.join(tracker, ticket_id)
    os.makedirs(ticket_dir, exist_ok=True)
    final_path = os.path.join(ticket_dir, event_filename(timestamp, uuid_str, event_type))
    relative_path = os.path.relpath(final_path, tracker)

    # Stage canonical bytes to a same-filesystem temp (atomic rename target).
    fd, staging = tempfile.mkstemp(prefix=".tmp-event-", dir=tracker)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_canonical_bytes(event))
    except OSError as exc:
        _silent_unlink(staging)
        raise StoreError("Error: failed to write staging temp file", 1) from exc

    _ensure_gc_auto_zero(tracker)
    commit_msg = f"ticket: {event_type} {ticket_id}"
    try:
        with _lock.write_lock(tracker, dual_window=True):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            try:
                os.replace(staging, final_path)  # atomic rename
            except OSError as exc:
                raise StoreError("Error: atomic rename failed", 1) from exc
            add = subprocess.run(
                ["git", "-C", tracker, "add", relative_path],
                capture_output=True,
                text=True,
            )
            commit = subprocess.run(
                ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg],
                capture_output=True,
                text=True,
            )
            if add.returncode != 0 or commit.returncode != 0:
                _silent_unlink(final_path)
                raise StoreError("Error: git commit failed while holding lock", 1)
    except (RebaseGuard, LockTimeout):
        _silent_unlink(staging)
        raise
    finally:
        _silent_unlink(staging)  # no-op once renamed
    return 0


def write_and_push(tracker: str | os.PathLike, ticket_id: str, event: dict[str, Any]) -> int:
    """Locked canonical commit, then the best-effort push (mirrors write_commit_event)."""
    rc = stage_and_commit(tracker, ticket_id, event)
    from rebar._store import push

    push.push_tickets_branch(_lock.canonical_tracker(tracker))
    return rc


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
