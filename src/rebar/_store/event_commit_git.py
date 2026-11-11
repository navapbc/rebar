"""Bounded Git verbs for locked event commits.

Every Git child launched by ``event_append.py`` while holding the store write
lock passes through this module. :func:`_run_git` applies one wall-clock bound and
returns timeout as a failed process result. The add, commit, remove, restore, and
unstage verbs compose shared index-lock recovery with transient filesystem retry
from ``gitutil.py``.

The claim and transition writer shares that retry machinery through
``_commands/txn.py``. ``event_append`` re-exports these names and calls them
through module globals so established monkeypatch seams continue to observe both
normal and recovery paths.
"""

from __future__ import annotations

import os
import subprocess

from rebar._store.gitutil import (
    _AUTOMAINT_OFF,
    _LOCAL_GIT_TIMEOUT,
    _with_index_lock_retry,
    _with_transient_fault_retry,
    run_git_bounded,
    run_git_reaped,
)

_GIT_ADD_ATTEMPTS = 3


# Shared ``gitutil`` index-lock recovery serves event, claim, and transition
# writers. ``_git_add`` composes it with transient filesystem retry
# (bug fix-indexlock-retry).


# Bound every Git child that runs while holding the store lock (c2ba), so a stalled
# volume fails the write and releases the lock instead of holding it forever. c2ba
# chose 30 s only to match push; every call here is local index work, whose cost
# grows with index entries, not with the network. A 213,713-entry store with a stale
# stat cache spent ~49 s per index refresh, so its commits timed out and the writes
# were lost (bug evolutive-descendent-manta). As ruled for sync (bug
# painted-epidermal-shelduck), local index work uses gitutil's local bound and
# network git keeps push's 30 s. The cost: a stalled volume holds the lock up to this
# bound per call. review_bot.config derives its force-exit grace from this value.
_GIT_TIMEOUT = _LOCAL_GIT_TIMEOUT


# raw-git-ok: locked store seam internal
def _run_git(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run captured text Git with :data:`_GIT_TIMEOUT`.

    This adapter preserves the historical argument-list interface while delegating
    to :func:`gitutil.run_git_bounded`. Timeout returns code ``124`` so callers and
    retry wrappers unwind the write lock through existing result handling. Launch
    ``OSError`` exceptions still propagate for best-effort callers to catch.
    """
    if argv[:2] == ["git", "-C"]:
        return run_git_bounded(argv[2], *argv[3:], timeout=_GIT_TIMEOUT)
    return run_git_bounded(None, *argv[1:], timeout=_GIT_TIMEOUT)


# raw-git-ok: locked store seam internal
def _git_add(
    tracker: str, relpaths: list[str], *, attempts: int = _GIT_ADD_ATTEMPTS
) -> subprocess.CompletedProcess[str]:
    """Stage *relpaths* with transient and index-lock recovery.

    Success and nontransient path, permission, or unmerged errors return on the
    first attempt. Idempotent adds retry transient object-store failures up to
    *attempts*. The outer index-lock helper waits for contention and reclaims stale
    locks. Return the final :class:`subprocess.CompletedProcess`.
    """

    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_fault_retry(
            lambda: _run_git(["git", "-C", tracker, "add", "--", *relpaths]),
            attempts=attempts,
        ),
        force_reclaim=True,
    )


# raw-git-ok: locked store seam internal
def _git_commit(tracker: str, commit_msg: str) -> subprocess.CompletedProcess[str]:
    """Commit with index-lock and transient filesystem recovery.

    The outer helper handles index contention and stale locks. The inner retry
    covers transient HEAD reads and loose-object writes. Other failures, including
    an empty or unmerged index, return immediately for caller recovery.
    ``_AUTOMAINT_OFF`` keeps repacking outside this bounded commit. The caller runs
    deferred maintenance under the same write lock (bd66).
    """
    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_fault_retry(
            lambda: _run_git(
                [
                    "git",
                    "-C",
                    tracker,
                    *_AUTOMAINT_OFF,
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    commit_msg,
                ]
            )
        ),
        force_reclaim=True,
    )


# raw-git-ok: locked store seam internal
def _git_rm(tracker: str, relpaths: list[str]) -> subprocess.CompletedProcess[str]:
    """``git -C tracker rm -q -- <relpaths>``, riding out index.lock contention (and
    reclaiming a stale lock) via :func:`_with_index_lock_retry`. Stages the deletions AND
    removes the worktree files; a non-lock failure surfaces immediately."""
    return _with_index_lock_retry(
        tracker,
        lambda: _run_git(["git", "-C", tracker, "rm", "-q", "--", *relpaths]),
        force_reclaim=True,
    )


# raw-git-ok: locked store seam internal
def _git_commit_paths(
    tracker: str, commit_msg: str, relpaths: list[str]
) -> subprocess.CompletedProcess[str]:
    """``git -C tracker commit -q --no-verify -m <msg> -- <relpaths>`` (a PATHSPEC-scoped
    partial commit), riding out index.lock contention via :func:`_with_index_lock_retry`.

    The pathspec is the point: unlike a bare ``git commit`` (which commits the WHOLE index),
    this commits ONLY *relpaths*, so it can never sweep an unrelated staged event — belt to
    the write lock's braces. Rides out index.lock contention AND the transient runner-FS
    git faults via the same composed gitutil retries as :func:`_git_commit`.

    ``_AUTOMAINT_OFF`` is injected so git's post-commit auto-maintenance repack is NOT charged
    to this bounded commit (bd66); the caller defers it to :func:`gitutil.run_auto_maintenance`."""
    argv = [
        "git",
        "-C",
        tracker,
        *_AUTOMAINT_OFF,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        commit_msg,
        "--",
        *relpaths,
    ]
    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_fault_retry(lambda: _run_git(argv)),
        force_reclaim=True,
    )


# raw-git-ok: locked store seam internal
def _restore_paths(tracker: str, relpaths: list[str]) -> None:
    """Restore *relpaths* to their committed HEAD state in both index and worktree
    (best-effort). Undoes a staged ``git rm`` whose commit then failed, so a failed delete
    leaves the store exactly as it was (the events stay present and committed)."""
    try:
        _run_git(["git", "-C", tracker, "checkout", "HEAD", "--", *relpaths])
    except OSError:
        pass


# raw-git-ok: locked store seam internal
def _unstage(tracker: str | os.PathLike, relative_path: str) -> None:
    """Drop a staged event from the git index (best-effort).

    An atomic rename followed by ``git add`` leaves the blob STAGED. If the write then
    fails, unlinking the worktree file alone is not enough: the blob stays in the index
    and the NEXT successful write (which commits the whole index) durably commits this
    failed write's phantom event. Mirrors ``_commands.txn._unstage`` — the claim/
    transition path already carries this fix; the general append path did not.
    """
    try:
        _run_git(["git", "-C", str(tracker), "reset", "-q", "--", relative_path])
    except OSError:
        pass


# git's incremental maintenance tasks (the ``maintenance.strategy=incremental`` set
# minus network prefetch). None walks reachability, so cost tracks new objects, not
# store size: on a copy of the 1.07M-object production store they took 0.35 s, and
# 17.3 s when incremental-repack fired, where ``gc --auto`` took 133.9 s and a full
# repack 605.6 s -- far past the bound below (bug nickelic-lazuline-gonolek).
# ``--auto`` keeps git's per-task thresholds, so a quiet store pays only the checks.
_MAINTENANCE_TASKS: tuple[str, ...] = (
    "--task=commit-graph",
    "--task=loose-objects",
    "--task=incremental-repack",
)


# raw-git-ok: locked store seam internal
def run_auto_maintenance(
    tracker: str | os.PathLike[str], *, timeout: float = _LOCAL_GIT_TIMEOUT
) -> subprocess.CompletedProcess | None:
    """Run explicit best-effort Git maintenance after commit (bd66).

    This is the store's only maintenance: every bounded Git child runs with implicit
    maintenance off (``gitutil._without_implicit_maintenance``). ADR 0051 requires the
    caller to hold the write lock, so the step is foreground and serialized. Git leads
    its own process group; a timeout reaps the whole group, so no repack outlives the
    lock and git releases its lock files. A durable write does not fail when maintenance
    fails or times out. Return the Git result, with timeout as code ``124``, or ``None``
    when Git cannot launch.
    """
    try:
        return run_git_reaped(
            tracker, "maintenance", "run", "--auto", "--quiet", *_MAINTENANCE_TASKS, timeout=timeout
        )
    except OSError:
        return None
