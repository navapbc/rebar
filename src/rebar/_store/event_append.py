"""Locked event transactions and commit recovery for the ticket store.

The in-process write path accepts a composed event, then atomically renames,
stages, and commits it under the unified write lock. :func:`write_and_push` adds
a best-effort push.

Pre-lock validation, canonical serialization, and staging live in
``event_prepare.py``. Bounded Git verbs with shared retry policy live in
``event_commit_git.py``. Both remain re-exported here to preserve imports and
monkeypatch targets. This module owns the lock bodies, batch rollback, push and
enrichment handoffs, and recovery from unmerged or missing index objects.
Recovery calls ``_git_add`` through this module so one test patch observes both
the initial add and recovery add.

This is the local ticket-store writer. The Jira reconciler is a client whose
inbound batcher is not the local batch API. See ``docs/architecture.md`` under
"Two writers, one store".

``StoreError.returncode`` preserves the former shell contract. Code ``1`` covers
lock timeout, rename failure, and commit failure with distinct diagnostics. Code
``75`` denotes the rebase or merge guard.
"""

from __future__ import annotations

import logging
import os

# ``subprocess`` is re-exported (redundant alias): the store suite patches
# ``event_append.subprocess.run``, and it is the SAME cached module object the relocated
# verbs in ``event_commit_git`` call, so that seam still reaches them.
import subprocess as subprocess
from collections.abc import Callable, Iterable
from typing import Any

from rebar._store import git_outcome
from rebar._store import lock as _lock
from rebar._store import staging as _staging

# Concern leaves, re-exported so the relocated verbs stay reachable through this module's
# globals for the store writer's established monkeypatch seams.
from rebar._store.event_commit_git import (
    _GIT_ADD_ATTEMPTS as _GIT_ADD_ATTEMPTS,
)
from rebar._store.event_commit_git import (
    _GIT_TIMEOUT as _GIT_TIMEOUT,
)
from rebar._store.event_commit_git import (
    _git_add,
    _git_commit,
    _git_commit_paths,
    _git_rm,
    _restore_paths,
    _run_git,
    _unstage,
    run_auto_maintenance,
)
from rebar._store.event_prepare import (
    EVENT_TYPES as EVENT_TYPES,
)
from rebar._store.event_prepare import (
    StoreError,
    _ensure_initialized,
    _prepare_event,
)
from rebar._store.event_prepare import (
    _validate_event as _validate_event,
)
from rebar._store.event_prepare import (
    canonical_bytes as canonical_bytes,
)
from rebar._store.event_prepare import (
    event_filename as event_filename,
)

# Used directly by the retained self-heal below — NOT part of the relocated verb set.
from rebar._store.gitutil import (
    _AUTOMAINT_OFF,
    _with_transient_fault_retry,
    discard_unmerged_paths,
    path_is_foreign_to_branch,
)

# Shared index.lock self-healing (bug fix-indexlock-retry). ``_INDEX_LOCK_STALE_S`` is
# re-exported here (redundant alias) because a test reads ``event_append._INDEX_LOCK_STALE_S``.
from rebar._store.gitutil import _INDEX_LOCK_STALE_S as _INDEX_LOCK_STALE_S
from rebar._store.lock import LockTimeout, RebaseGuard  # re-export for callers

_log = logging.getLogger(__name__)


def _deferred_maintenance(tracker: str | os.PathLike) -> None:
    """Run best-effort Git maintenance after commit under the caller's lock.

    Commits suppress automatic maintenance so ``_GIT_TIMEOUT`` covers only the
    commit. This foreground step uses the longer local watchdog while preserving
    ADR 0051 serialization. The durable write survives maintenance failure.
    """
    run_auto_maintenance(tracker)


def delete_events(tracker: str | os.PathLike, relpaths: Iterable[str], commit_msg: str) -> int:
    """Delete selected committed events under the unified write lock.

    Retention prunes check the rebase guard, apply ``git rm`` to exactly
    *relpaths*, and use a pathspec commit so unrelated staged events cannot enter
    the prune commit. Shared retry handles index-lock contention. Commit failure
    restores every deletion to HEAD.

    Return the number deleted. An empty input acquires no lock and commits nothing.
    Raise :class:`StoreError` or :class:`LockTimeout` with code ``1`` and
    :class:`RebaseGuard` with code ``75``. Sidecar callers own any best-effort
    exception policy.
    """
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    paths = [r for r in relpaths if r]
    if not paths:
        return 0
    with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
        _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
        rm = _git_rm(tracker, paths)
        if rm.returncode != 0:
            rm_err = (rm.stderr or rm.stdout).strip()
            raise StoreError(
                "Error: git rm failed while holding lock" + (f": {rm_err}" if rm_err else ""),
                1,
            )
        commit = _git_commit_paths(tracker, commit_msg, paths)
        if commit.returncode != 0:
            _restore_paths(tracker, paths)  # leave the store as it was
            git_err = (commit.stderr or commit.stdout).strip()
            raise StoreError(
                "Error: git commit failed while holding lock" + (f": {git_err}" if git_err else ""),
                1,
            )
        _deferred_maintenance(tracker)
    return len(paths)


def stage_and_commit(
    tracker: str | os.PathLike,
    ticket_id: str,
    event: dict[str, Any],
    *,
    under_lock_check: Callable[[], None] | None = None,
) -> int:
    """Validate, canonical-stage, lock, atomic-rename, ``git add``+``commit``.

    Returns 0 on success; raises :class:`StoreError` (1), :class:`RebaseGuard` (75),
    or :class:`LockTimeout` (1) with the exact bash stderr."""
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    staged = _prepare_event(tracker, ticket_id, event)

    event_type = str(event["event_type"]).upper()
    commit_msg = f"ticket: {event_type} {ticket_id}"
    try:
        with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
            _lock.check_no_rebase_in_progress(tracker)  # raises RebaseGuard (75)
            if under_lock_check is not None:
                under_lock_check()
            try:
                staged.promote()  # atomic publish (dir+event together for a new ticket)
            except OSError as exc:
                raise StoreError("Error: atomic rename failed", 1) from exc
            add = _git_add(tracker, [staged.relative_path])
            if add.returncode != 0:
                # Check add before the whole-index commit. On failure, reset both
                # index and worktree state so no partial event enters a later commit.
                _unstage(tracker, staged.relative_path)
                _silent_unlink(staged.final_path)
                staged.unpublish()
                # Preserve the established error phrase while appending Git diagnostics
                # needed to distinguish HEAD parsing and index-lock failures (bug edf7).
                add_err = (add.stderr or add.stdout).strip()
                raise StoreError(
                    "Error: git commit failed while holding lock"
                    + (f": {add_err}" if add_err else ""),
                    1,
                )
            commit = _git_commit(tracker, commit_msg)
            if commit.returncode != 0:
                # An existing unmerged index entry blocks every commit. Restore
                # regenerable bridge state and retry, but diagnose ticket data (bug 6818).
                healed, detail = _recover_from_unmerged(tracker, [staged.relative_path], commit_msg)
                if not healed and detail is None:
                    # A missing staged object poisons later commits until the index is
                    # rebuilt from HEAD (bug 4c1c, Mode D).
                    healed = _recover_from_invalid_object(
                        tracker, [staged.relative_path], commit_msg, commit.stderr or commit.stdout
                    )
                if not healed:
                    # Drop the staged blob from the index too (not just disk) so the failed
                    # event cannot be committed by the next successful write.
                    _unstage(tracker, staged.relative_path)
                    _silent_unlink(staged.final_path)
                    staged.unpublish()
                    git_err = (commit.stderr or commit.stdout).strip()
                    raise StoreError(
                        detail
                        or (
                            "Error: git commit failed while holding lock"
                            + (f": {git_err}" if git_err else "")
                        ),
                        1,
                    )
            _deferred_maintenance(tracker)
    except (RebaseGuard, LockTimeout):
        staged.discard()
        raise
    finally:
        staged.discard()  # no-op once published
    return 0


def _prepare_batch(
    tracker: str, items: Iterable[tuple[str, dict[str, Any]]]
) -> list[_staging.StagedEvent]:
    """Validate and stage a batch, discarding every prior temp if one item fails."""
    prepared: list[_staging.StagedEvent] = []
    swept_stale = False
    try:
        for ticket_id, event in items:
            staged = _prepare_event(tracker, ticket_id, event, sweep_stale=not swept_stale)
            prepared.append(staged)
            if staged.staging_dir is not None:
                swept_stale = True
    except BaseException:
        for staged in prepared:
            staged.discard()
        raise
    return prepared


def _commit_prepared_batch_under_lock(
    tracker: str, prepared: list[_staging.StagedEvent], commit_msg: str
) -> int:
    """Promote and commit a prepared batch while the caller holds the unified write lock."""
    _lock.check_no_rebase_in_progress(tracker)
    relpaths = [staged.relative_path for staged in prepared]
    renamed: list[_staging.StagedEvent] = []
    try:
        for staged in prepared:
            try:
                staged.promote()
            except OSError as exc:
                raise StoreError("Error: atomic rename failed", 1) from exc
            renamed.append(staged)
        add = _git_add(tracker, relpaths)
        if add.returncode != 0:
            add_err = (add.stderr or add.stdout).strip()
            raise StoreError(
                "Error: git commit failed while holding lock" + (f": {add_err}" if add_err else ""),
                1,
            )
        commit = _git_commit(tracker, commit_msg)
        if commit.returncode != 0:
            healed, detail = _recover_from_unmerged(tracker, relpaths, commit_msg)
            if not healed and detail is None:
                healed = _recover_from_invalid_object(
                    tracker, relpaths, commit_msg, commit.stderr or commit.stdout
                )
            if not healed:
                git_err = (commit.stderr or commit.stdout).strip()
                raise StoreError(
                    detail
                    or (
                        "Error: git commit failed while holding lock"
                        + (f": {git_err}" if git_err else "")
                    ),
                    1,
                )
    except BaseException:
        _rollback_batch(tracker, renamed)
        raise
    _deferred_maintenance(tracker)
    return len(prepared)


def batch_stage_and_commit_under_lock(
    tracker: str | os.PathLike,
    items: Iterable[tuple[str, dict[str, Any]]],
    *,
    commit_msg: str | None = None,
) -> int:
    """Commit a batch without acquiring the write lock; the caller MUST already hold it.

    This is the composition seam for transactions that must re-read state and publish
    several events in one critical section. Validation, canonical staging, rollback, git
    recovery, and commit bytes are identical to :func:`batch_stage_and_commit`; only lock
    ownership differs. Never call it from an unlocked write path.
    """
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    prepared = _prepare_batch(tracker, items)
    if not prepared:
        return 0
    try:
        message = commit_msg or f"ticket: batch {len(prepared)} events"
        return _commit_prepared_batch_under_lock(tracker, prepared, message)
    finally:
        for staged in prepared:
            staged.discard()


def batch_stage_and_commit(
    tracker: str | os.PathLike, items: Iterable[tuple[str, dict[str, Any]]]
) -> int:
    """Commit MANY events under ONE lock acquire + ONE ``git commit`` (all-or-nothing).

    Events are validated and canonical-staged before lock acquisition. Under the unified
    lock they are promoted, path-scoped into one commit, and rolled back as a unit on any
    rename/index/commit fault. An empty batch is a no-op. The under-existing-lock variant
    is :func:`batch_stage_and_commit_under_lock`.
    """
    tracker = _lock.canonical_tracker(tracker)
    _ensure_initialized(tracker)
    prepared = _prepare_batch(tracker, items)

    if not prepared:
        return 0

    commit_msg = f"ticket: batch {len(prepared)} events"
    try:
        with _lock.write_lock(tracker, dual_window=True, retries=_lock.write_path_retries()):
            return _commit_prepared_batch_under_lock(tracker, prepared, commit_msg)
    finally:
        for staged in prepared:
            staged.discard()


def write_and_push(
    tracker: str | os.PathLike,
    ticket_id: str,
    event: dict[str, Any],
    *,
    under_lock_check: Callable[[], None] | None = None,
) -> int:
    """Locked canonical commit, then the best-effort push (mirrors write_commit_event)."""
    rc = stage_and_commit(tracker, ticket_id, event, under_lock_check=under_lock_check)
    from rebar._store import push

    canonical = _lock.canonical_tracker(tracker)
    push.push_tickets_branch(canonical)
    # Warn when the store trails the ensure registry at the shared append and
    # composer choke point. This lazy, best-effort nudge cannot affect the commit.
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
    """Commit one batch under one lock, then perform one best-effort push.

    Empty input commits and pushes nothing. Return the number of committed events.
    """
    n = batch_stage_and_commit(tracker, items)
    if n:
        from rebar._store import push

        push.push_tickets_branch(_lock.canonical_tracker(tracker))
    return n


def _rollback_batch(tracker: str, renamed: list[_staging.StagedEvent]) -> None:
    """Remove every published event from a failed batch.

    Unstage and unlink each event so no later commit captures it. Then remove any
    ticket directory created by the batch after its event is gone (ticket 021d).
    """
    for staged in renamed:
        _unstage(tracker, staged.relative_path)
    for staged in renamed:
        _silent_unlink(staged.final_path)
    for staged in renamed:
        staged.unpublish()


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


# Reconciler-managed bridge-state files are REGENERABLE (the reconciler rebuilds them
# on its next pass; a missing/empty one just forces a full re-fetch), so a stranded
# conflict on them can be safely resolved to HEAD. Other paths are real ticket data.
_REGENERABLE_PREFIX = ".bridge_state/"


# raw-git-ok: locked store seam internal
def _recover_from_unmerged(
    tracker: str, event_relpaths: list[str], commit_msg: str
) -> tuple[bool, str | None]:
    """Recover a commit blocked by an existing unmerged index entry (bug 6818).

    Clear reconciler-regenerable or branch-foreign paths and retry. Return
    ``(True, None)`` on success. Return ``(False, detail)`` for tracked ticket
    data that requires operator resolution. Return ``(False, None)`` when no
    unmerged path exists or the retry fails.
    """
    unmerged = _run_git(
        ["git", "-C", tracker, "diff", "--name-only", "--diff-filter=U"]
    ).stdout.split()
    if not unmerged:
        return (False, None)
    regen = [p for p in unmerged if p.startswith(_REGENERABLE_PREFIX)]
    rest = [p for p in unmerged if p not in regen]
    # A branch-foreign path cannot be ticket data, so discard it with regenerable
    # state instead of blocking all writes (bug 2fa6).
    foreign = [p for p in rest if path_is_foreign_to_branch(tracker, p)]
    ticket_data = [p for p in rest if p not in foreign]
    if ticket_data:
        return (
            False,
            "Error: git commit blocked by unmerged path(s) in the tracker index: "
            f"{', '.join(ticket_data)} — the tickets worktree has a stranded merge/stash "
            "conflict. Resolve it (e.g. `git -C <tracker> checkout HEAD -- <path>`) and retry.",
        )
    discard_unmerged_paths(tracker, regen, foreign)
    _git_add(tracker, list(event_relpaths))
    # Apply the commit path's transient retry because recovery also reads HEAD and
    # writes loose objects.
    retry = _with_transient_fault_retry(
        lambda: _run_git(
            ["git", "-C", tracker, *_AUTOMAINT_OFF, "commit", "-q", "--no-verify", "-m", commit_msg]
        )
    )
    return (retry.returncode == 0, None)


# ``write-tree`` rejects an index entry whose object is absent from the object
# database. The shared classifier gives this signature its own kind because it
# invokes the recovery below instead of the terminal failure path.
_INVALID_OBJECT_MARKERS = git_outcome.INVALID_OBJECT_MARKERS


def _is_invalid_object_error(text: str) -> bool:
    """Return whether Git rejected a commit for a missing indexed object.

    This delegates to the shared :mod:`rebar._store.git_outcome` classifier.
    """
    return git_outcome.is_invalid_object(text or "")


def _staged_index_paths(tracker: str) -> list[str]:
    """Paths currently staged in the index. ``ls-files --cached`` reads ``.git/index``
    directly (no object access), so it is safe to call on a poisoned index."""
    r = _run_git(["git", "-C", tracker, "ls-files", "--cached"])
    return r.stdout.splitlines() if r.returncode == 0 else []


# raw-git-ok: locked store seam internal
def _recover_from_invalid_object(
    tracker: str, event_relpaths: list[str], commit_msg: str, commit_stderr: str
) -> bool:
    """Recover a commit whose index references a missing object (bug 4c1c).

    A vanished loose object can leave an earlier path staged and make every later
    commit fail. Per-path unstage cannot remove an entry owned by that earlier
    write. Under the write lock, ``read-tree HEAD`` rebuilds the entire index
    without changing worktree event files. Re-stage this write to regenerate its
    object, then retry the commit.

    Return ``True`` only when the retry commits. A different failure returns
    ``False`` without changing the index.
    """
    if not _is_invalid_object_error(commit_stderr):
        return False
    # Record every recovery because this signature can indicate a leaked index,
    # external writer, vanished object, or recurring storage fault. Capture staged
    # paths directly from the index so the warning can name reset orphans.
    staged_before = _staged_index_paths(tracker)
    # HEAD must exist for read-tree HEAD; the cascade only arises after prior writes, so it
    # always does. If it somehow doesn't, the retry commit simply fails → the caller raises.
    read_tree = _run_git(["git", "-C", tracker, "read-tree", "HEAD"])
    _git_add(tracker, list(event_relpaths))
    retry = _with_transient_fault_retry(
        lambda: _run_git(
            ["git", "-C", tracker, *_AUTOMAINT_OFF, "commit", "-q", "--no-verify", "-m", commit_msg]
        )
    )
    healed = retry.returncode == 0
    # An orphan is an EARLIER failed write's file dropped from the index by the reset but left
    # in the worktree — visible to local replay yet uncommitted/unpushed. Name it so a real
    # local↔remote divergence is observable rather than silent (follow-up: reconcile it).
    orphaned = sorted(set(staged_before) - set(event_relpaths))
    _log.warning(
        "self-healed a poisoned index (invalid/missing object): read_tree_ok=%s healed=%s%s",
        read_tree.returncode == 0,
        healed,
        f" orphaned_worktree_paths={orphaned}" if orphaned else "",
    )
    return healed
