"""Canonical locked event-commit for the tickets store.

In-process replacement for the bash write path
``ticket-append-event.sh`` → ``write_commit_event`` → ``_flock_stage_commit``: takes
a fully-composed event dict (the seam already builds ``{timestamp, uuid,
event_type, env_id, author, data}``), serialises it to the CANONICAL committed
bytes, stages it same-filesystem, and under the unified write lock does the atomic
rename + ``git add`` + ``git commit``. ``write_and_push`` additionally runs the
best-effort push.

**Scope — this is the LOCAL ticket-store write path.** The Jira reconciler
(``rebar_reconciler/``) is a separate *client* of this store; its inbound
commit-batcher is a Jira-sync internal, NOT the general local batch-write API. Do
not conflate the two — see ``docs/architecture.md`` "Two writers, one store".

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
import time
from collections.abc import Iterable
from typing import Any

from rebar._store import lock as _lock
from rebar._store.canonical import canonical_bytes  # the single canonical serializer

# Shared index.lock self-healing (bug fix-indexlock-retry). ``_INDEX_LOCK_STALE_S`` is
# re-exported here (redundant alias) because a test reads ``event_append._INDEX_LOCK_STALE_S``.
from rebar._store.gitutil import _INDEX_LOCK_STALE_S as _INDEX_LOCK_STALE_S
from rebar._store.gitutil import _with_index_lock_retry
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
            "REVIEW_RESULT, TICKET_DIGEST, ENQUEUE_ENRICH, CLAIM_ENRICH, DONE_ENRICH",
            1,
        )
    return event_type, timestamp, uuid_str


def _prepare_event(tracker: str, ticket_id: str, event: dict[str, Any]) -> tuple[str, str, str]:
    """Validate the event, ensure its ticket dir, and stage its CANONICAL bytes to a
    same-filesystem temp (the atomic-rename source). Returns ``(staging, final_path,
    relative_path)``. Raises :class:`StoreError` (1); no lock is held here."""
    event_type, timestamp, uuid_str = _validate_event(event)
    ticket_dir = os.path.join(tracker, ticket_id)
    os.makedirs(ticket_dir, exist_ok=True)
    final_path = os.path.join(ticket_dir, event_filename(timestamp, uuid_str, event_type))
    relative_path = os.path.relpath(final_path, tracker)
    fd, staging = tempfile.mkstemp(prefix=".tmp-event-", dir=tracker)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(canonical_bytes(event))
    except OSError as exc:
        _silent_unlink(staging)
        raise StoreError("Error: failed to write staging temp file", 1) from exc
    return staging, final_path, relative_path


# git's object database write intermittently fails on CI runners while hashing a blob
# during ``git add``: the loose-object temp create under ``.git/objects/`` returns
# ENOENT (Linux: "unable to create temporary file: No such file or directory") or
# EINVAL (macOS: "… Invalid argument"), surfaced as "failed to insert into database" /
# "unable to index file" / "fatal: adding files failed". It is a transient
# filesystem hiccup, NOT a data fault — the identical add succeeds on retry (a Gerrit
# ``recheck`` on the same patchset passes). Retrying ONLY this signature turns a
# runner-FS blip from a hard write failure that red-lights unrelated CI into a
# self-healed write. Bugs vocal-dip-robin / brainy-floral-globefish.
_TRANSIENT_ADD_MARKERS = (
    "unable to create temporary file",
    "failed to insert into database",
    "unable to index file",
)
_GIT_ADD_ATTEMPTS = 3
_GIT_ADD_BACKOFF_S = 0.1


def _is_transient_add_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _TRANSIENT_ADD_MARKERS)


# git's index.lock self-healing (constants + ``_is_index_lock_error`` +
# ``_reclaim_if_stale_index_lock`` + ``_with_index_lock_retry``) now lives in the SHARED
# ``rebar._store.gitutil`` so the claim/transition write path (txn.py) self-heals through the
# same implementation (bug fix-indexlock-retry). Imported at module top; ``_INDEX_LOCK_STALE_S``
# is re-exported there for tests. ``_git_add`` below composes gitutil's index.lock retry with
# event_append's OWN object-DB ``git add`` retry (the ``_TRANSIENT_ADD_MARKERS`` loop).


def _git_add(
    tracker: str, relpaths: list[str], *, attempts: int = _GIT_ADD_ATTEMPTS
) -> subprocess.CompletedProcess[str]:
    """``git -C tracker add -- <relpaths>``, retrying transient object-DB AND index.lock
    failures.

    On success or a NON-transient failure returns immediately (behavior unchanged — a
    real pathspec/permission/UU error still surfaces on the first attempt). On the
    transient object-DB signature the identical add is retried up to *attempts* times
    with a short backoff, because re-adding the same paths is idempotent and the fault
    clears on retry; index.lock contention is ridden out (and a stale lock reclaimed) by
    :func:`_with_index_lock_retry`. Returns the final :class:`subprocess.CompletedProcess`."""

    def _add_once() -> subprocess.CompletedProcess[str]:
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, attempts + 1):
            result = subprocess.run(
                ["git", "-C", tracker, "add", "--", *relpaths],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result
            if attempt < attempts and _is_transient_add_error(result.stderr or result.stdout or ""):
                time.sleep(_GIT_ADD_BACKOFF_S * attempt)
                continue
            return result
        assert result is not None  # attempts >= 1, so the loop body ran at least once
        return result

    return _with_index_lock_retry(tracker, _add_once)


def _git_commit(tracker: str, commit_msg: str) -> subprocess.CompletedProcess[str]:
    """``git -C tracker commit -q --no-verify -m <msg>``, riding out index.lock
    contention (and reclaiming a stale lock) via :func:`_with_index_lock_retry`. A
    non-lock failure (including a genuine "nothing to commit" / UU wedge) surfaces
    immediately, unchanged — the caller's UU-recovery path still handles it."""
    return _with_index_lock_retry(
        tracker,
        lambda: subprocess.run(
            ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg],
            capture_output=True,
            text=True,
        ),
    )


def stage_and_commit(tracker: str | os.PathLike, ticket_id: str, event: dict[str, Any]) -> int:
    """Validate, canonical-stage, lock, atomic-rename, ``git add``+``commit``.

    Returns 0 on success; raises :class:`StoreError` (1), :class:`RebaseGuard` (75),
    or :class:`LockTimeout` (1) with the exact bash stderr."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    staging, final_path, relative_path = _prepare_event(tracker, ticket_id, event)

    event_type = str(event["event_type"]).upper()
    commit_msg = f"ticket: {event_type} {ticket_id}"
    try:
        with _lock.write_lock(tracker, dual_window=True):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            try:
                os.replace(staging, final_path)  # atomic rename
            except OSError as exc:
                raise StoreError("Error: atomic rename failed", 1) from exc
            add = _git_add(tracker, [relative_path])
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
            commit = _git_commit(tracker, commit_msg)
            if commit.returncode != 0:
                # A pre-existing unmerged (UU) index entry — e.g. a stranded stash/merge
                # conflict on a reconciler-regenerable .bridge_state/* file (bug 6818) —
                # makes git refuse the commit entirely. Self-heal regenerable paths to
                # HEAD and retry; surface an actionable error for a non-regenerable one.
                healed, detail = _recover_from_unmerged(tracker, [relative_path], commit_msg)
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


def batch_stage_and_commit(
    tracker: str | os.PathLike, items: Iterable[tuple[str, dict[str, Any]]]
) -> int:
    """Commit MANY events under ONE lock acquire + ONE ``git commit`` (all-or-nothing).

    *items* is an iterable of ``(ticket_id, event)`` pairs. Every event is validated
    and canonical-staged (the same byte contract as :func:`stage_and_commit`) BEFORE
    the lock is taken; then, holding the single write lock (I5) exactly ONCE, each
    staged temp is atomically renamed into its I2 path (``{ticket}/{ts}-{uuid}-{TYPE}
    .json``), a single ``git add`` stages them all, and ONE ``git commit`` seals the
    batch. Returns the number of events committed (``0`` for an empty batch — a no-op
    that takes no lock and makes no commit).

    **Batch atomicity is all-or-nothing per commit.** Because replay/dedup/union-merge
    /compaction key off each event's UUID and NOT commit boundaries, collapsing N
    commits into 1 is invisible to readers — but a *partial* batch is not. So any
    failure (validation, rename, ``git add``, or a non-recoverable ``git commit``)
    rolls the batch back completely: every already-renamed final is unstaged from the
    index AND unlinked from the worktree, leaving the store exactly as it was. A
    crash mid-batch (before the commit) leaves at most orphaned worktree files that
    are never in any commit; the next writer's ``git add``/``commit`` only touches its
    own paths, and a re-run re-emits the whole batch. The lock is NOT re-entrant, so
    this MUST acquire it once and loop the renames inside — it never calls
    :func:`stage_and_commit` per event.

    Raises :class:`StoreError` (1), :class:`RebaseGuard` (75), or :class:`LockTimeout`
    (1) with the exact bash stderr, same as the single-event path."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)

    # Validate + stage every event up front (fail fast, before the lock). On any
    # failure here, unlink the temps staged so far — nothing has been renamed yet.
    prepared: list[tuple[str, str, str]] = []  # (staging, final_path, relative_path)
    try:
        for ticket_id, event in items:
            prepared.append(_prepare_event(tracker, ticket_id, event))
    except BaseException:
        for staging, _final, _rel in prepared:
            _silent_unlink(staging)
        raise

    if not prepared:
        return 0

    commit_msg = f"ticket: batch {len(prepared)} events"
    relpaths = [rel for _s, _f, rel in prepared]
    renamed: list[tuple[str, str]] = []  # (final_path, relative_path) already in place
    try:
        with _lock.write_lock(tracker, dual_window=True):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            for staging, final_path, relative_path in prepared:
                try:
                    os.replace(staging, final_path)  # atomic rename
                except OSError as exc:
                    _rollback_batch(tracker, renamed)
                    raise StoreError("Error: atomic rename failed", 1) from exc
                renamed.append((final_path, relative_path))
            add = _git_add(tracker, relpaths)
            if add.returncode != 0:
                _rollback_batch(tracker, renamed)
                add_err = (add.stderr or add.stdout).strip()
                raise StoreError(
                    "Error: git commit failed while holding lock"
                    + (f": {add_err}" if add_err else ""),
                    1,
                )
            commit = _git_commit(tracker, commit_msg)
            if commit.returncode != 0:
                healed, detail = _recover_from_unmerged(tracker, relpaths, commit_msg)
                if not healed:
                    _rollback_batch(tracker, renamed)
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
        for staging, _final, _rel in prepared:
            _silent_unlink(staging)
        raise
    finally:
        for staging, _final, _rel in prepared:
            _silent_unlink(staging)  # no-op once renamed
    return len(prepared)


def write_and_push(tracker: str | os.PathLike, ticket_id: str, event: dict[str, Any]) -> int:
    """Locked canonical commit, then the best-effort push (mirrors write_commit_event)."""
    rc = stage_and_commit(tracker, ticket_id, event)
    from rebar._store import push

    canonical = _lock.canonical_tracker(tracker)
    push.push_tickets_branch(canonical)
    # Best-effort, fail-silent write-path nudge that an existing store is behind the
    # idempotent ensure-registry (epic odd-vortex-elbow / WS2). This is the single
    # choke point through which _seam.append_event (comment/tag/edit/link/set_*/sign)
    # and the composer create/edit/revert path funnel; the nudge NEVER affects the
    # (already-committed) write. Lazy import so the read path stays untouched.
    try:
        from rebar._store import ensures as _ensures

        _ensures.maybe_emit_pending_hint(canonical)
    except Exception:  # noqa: BLE001 — the hint must never fail a committed write
        pass
    # Opportunistic cross-ticket enrichment drain (epic only-crave-art / c1de): a cheap
    # gate that no-ops unless something is soaked. Best-effort — never fails the write.
    _maybe_enrich_drain(str(canonical))
    return rc


def batch_write_and_push(
    tracker: str | os.PathLike, items: Iterable[tuple[str, dict[str, Any]]]
) -> int:
    """Batched commit (:func:`batch_stage_and_commit`), then ONE best-effort push.

    The bulk analogue of :func:`write_and_push`: instead of one push per event, the
    whole batch commits under a single lock and a single push follows. An empty batch
    commits nothing and skips the push. Returns the number of events committed."""
    n = batch_stage_and_commit(tracker, items)
    if n:
        from rebar._store import push

        push.push_tickets_branch(_lock.canonical_tracker(tracker))
    return n


def _rollback_batch(tracker: str, renamed: list[tuple[str, str]]) -> None:
    """Undo a failed batch: unstage every already-renamed event from the index and
    unlink it from the worktree, so a partial batch leaves NO phantom event (neither
    staged nor committable by the next write) and no orphaned worktree file."""
    for _final_path, relative_path in renamed:
        _unstage(tracker, relative_path)
    for final_path, _relative_path in renamed:
        _silent_unlink(final_path)


def _maybe_enrich_drain(tracker: str) -> None:
    """Ride the write path with the opportunistic enrichment drain gate. Fully isolated: a
    missing [agents] extra or any failure is a clean no-op (never fails the triggering write)."""
    try:
        from rebar.llm.enrich_drain import maybe_drain

        maybe_drain(tracker)
    except Exception:  # noqa: BLE001 — a drain concern must never fail a write; broad-but-swallowed
        pass


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
    tracker: str, event_relpaths: list[str], commit_msg: str
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
    _git_add(tracker, list(event_relpaths))
    retry = subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg],
        capture_output=True,
        text=True,
    )
    return (retry.returncode == 0, None)
