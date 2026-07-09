"""Canonical locked event-commit for the tickets store.

In-process replacement for the bash write path
``ticket-append-event.sh`` → ``write_commit_event`` → ``_flock_stage_commit``: takes
a fully-composed event dict (the seam already builds ``{timestamp, uuid,
event_type, env_id, author, data}``), serialises it to the CANONICAL committed
bytes, stages it same-filesystem, and under the unified write lock does the atomic
rename + ``git add`` + ``git commit``. ``write_and_push`` additionally runs the
best-effort push.

**Byte parity (the contract).** The committed bytes come from the single canonical
serializer :func:`rebar._store.canonical.canonical_bytes`
(``json.dumps(event, ensure_ascii=False, separators=(',', ':'), sort_keys=True)`` with
NO trailing newline), shared by every live event writer and pinned Python↔Python by
``tests/interfaces/store/test_canonical_event_bytes.py`` (and the byte contract +
structural guard in ``tests/unit/test_canonical.py``). This committer serialises the
*given* dict; it never re-derives author/env_id/uuid/timestamp (those are the seam's).

Exit-code parity (surfaced as ``StoreError.returncode`` → the seam's ``CommandError``):
``1`` lock timeout / atomic-rename failure / git-commit failure (distinct stderr each),
``75`` rebase/merge guard. Mirrors ``_flock_stage_commit`` (which maps its internal
2/3 to an external return 1).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from rebar._store import lock as _lock
from rebar._store.canonical import canonical_bytes  # the single canonical serializer
from rebar._store.lock import LockTimeout, RebaseGuard  # re-export for callers
from rebar.reducer._version import TAG_DELTA  # single source of truth for the type name

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
        # Plan-review observability sidecar (epic 5fd2 / child db7b). Reducer-IGNORED
        # (NOT in KNOWN_EVENT_TYPES) so it never enters compiled state / hot paths and
        # compaction preserves it; it is in this WRITE allow-list so it can be emitted.
        "REVIEW_RESULT",
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
            f"VERIFY_COMMANDS, SIGNATURE, WORKFLOW_RUN, WORKFLOW_STEP, COMMITS, {TAG_DELTA}, "
            "REVIEW_RESULT",
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
            fh.write(canonical_bytes(event))
    except OSError as exc:
        _silent_unlink(staging)
        raise StoreError("Error: failed to write staging temp file", 1) from exc

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
            if add.returncode != 0:
                # Check add's return code BEFORE running commit (audit 2.2): the commit
                # below commits the whole index, so running it after a failed add could
                # sweep unrelated staged residue in under THIS write's message. Reset the
                # index as well as unlinking the worktree file so the (possibly partially)
                # staged event cannot leak into the next successful write's commit.
                _unstage(tracker, relative_path)
                _silent_unlink(final_path)
                # Surface git's real stderr. The create path historically hid it behind
                # this generic message, leaving intermittent CI git races (bug edf7 —
                # "could not parse HEAD" / index.lock contention) undiagnosable; the
                # transition path (txn.py) already includes stderr. The recognizable
                # phrase is kept as a substring for anything matching on it.
                add_err = (add.stderr or add.stdout).strip()
                raise StoreError(
                    "Error: git commit failed while holding lock"
                    + (f": {add_err}" if add_err else ""),
                    1,
                )
            commit = subprocess.run(
                ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg],
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                # A pre-existing unmerged (UU) index entry — e.g. a stranded stash/merge
                # conflict on a reconciler-regenerable .bridge_state/* file (bug 6818) —
                # makes git refuse the commit entirely. Self-heal regenerable paths to
                # HEAD and retry; surface an actionable error for a non-regenerable one.
                healed, detail = _recover_from_unmerged(tracker, relative_path, commit_msg)
                if not healed:
                    # Drop the staged blob from the index too (not just disk) so the failed
                    # event cannot be committed by the next successful write.
                    _unstage(tracker, relative_path)
                    _silent_unlink(final_path)
                    git_err = (commit.stderr or commit.stdout).strip()
                    raise StoreError(
                        detail
                        or (
                            "Error: git commit failed while holding lock"
                            + (f": {git_err}" if git_err else "")
                        ),
                        1,
                    )
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
    # Best-effort, fail-silent write-path nudge that an existing store is behind the
    # idempotent ensure-registry (epic odd-vortex-elbow / WS2). This is the single
    # choke point through which _seam.append_event (comment/tag/edit/link/set_*/sign)
    # and the composer create/edit/revert path funnel; the nudge NEVER affects the
    # (already-committed) write. Lazy import so the read path stays untouched.
    try:
        from rebar._store import ensures as _ensures

        _ensures.maybe_emit_pending_hint(_lock.canonical_tracker(tracker))
    except Exception:  # noqa: BLE001 — the hint must never fail a committed write
        pass
    return rc


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _unstage(tracker: str | os.PathLike, relative_path: str) -> None:
    """Drop a staged event from the git index (best-effort).

    An atomic rename followed by ``git add`` leaves the blob STAGED. If the write then
    fails, unlinking the worktree file alone is not enough: the blob stays in the index
    and the NEXT successful write (which commits the whole index) durably commits this
    failed write's phantom event. Mirrors ``_commands.txn._unstage`` — the claim/
    transition path already carries this fix; the general append path did not.
    """
    try:
        subprocess.run(
            ["git", "-C", str(tracker), "reset", "-q", "--", relative_path],
            capture_output=True,
            text=True,
        )
    except OSError:
        pass


# Reconciler-managed bridge-state files are REGENERABLE (the reconciler rebuilds them
# on its next pass; a missing/empty one just forces a full re-fetch), so a stranded
# conflict on them can be safely resolved to HEAD. Other paths are real ticket data.
_REGENERABLE_PREFIX = ".bridge_state/"


def _recover_from_unmerged(
    tracker: str, event_relpath: str, commit_msg: str
) -> tuple[bool, str | None]:
    """Recover a commit that ``git`` refused because of a PRE-EXISTING unmerged (UU)
    index entry (bug 6818). A stranded stash/merge conflict leaves an unmerged index
    that blocks EVERY ``git commit``, wedging all store writes.

    Returns ``(healed, detail)``:
    - ``(True, None)`` — all unmerged paths were reconciler-regenerable; they were
      restored to HEAD and the event commit was retried successfully.
    - ``(False, <actionable message>)`` — a NON-regenerable path is unmerged (real
      ticket data, never auto-discarded); the caller raises with that message.
    - ``(False, None)`` — no unmerged paths (the commit failed for another reason) or
      the retry still failed; the caller raises the generic error.
    """
    unmerged = subprocess.run(
        ["git", "-C", tracker, "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if not unmerged:
        return (False, None)
    nonregen = [p for p in unmerged if not p.startswith(_REGENERABLE_PREFIX)]
    if nonregen:
        return (
            False,
            "Error: git commit blocked by unmerged path(s) in the tracker index: "
            f"{', '.join(nonregen)} — the tickets worktree has a stranded merge/stash "
            "conflict. Resolve it (e.g. `git -C <tracker> checkout HEAD -- <path>`) and retry.",
        )
    # All unmerged paths are regenerable → restore to HEAD and retry the event commit
    # once. Drop the unmerged index entries (all stages) THEN restore from HEAD: this
    # reliably clears both the UU and the working-tree markers (a bare `checkout HEAD
    # --` can leave a stranded stage behind).
    subprocess.run(
        ["git", "-C", tracker, "rm", "-q", "--cached", "--", *unmerged],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", tracker, "checkout", "HEAD", "--", *unmerged], capture_output=True, text=True
    )
    subprocess.run(["git", "-C", tracker, "add", event_relpath], capture_output=True, text=True)
    retry = subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg],
        capture_output=True,
        text=True,
    )
    return (retry.returncode == 0, None)
