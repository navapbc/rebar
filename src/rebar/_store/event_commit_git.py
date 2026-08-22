"""The git verbs the locked event-commit issues — bounded and retry-composed.

The second of the three concerns the store's write path is split into. Every git child
that ``_store/event_append.py`` launches WHILE HOLDING the store's MKDIR write lock is
issued from here, so the whole lock-held git surface carries one wall-clock bound and one
retry policy instead of a per-call-site assortment.

Two layers:

- :func:`_run_git` — the single ``subprocess`` entry point, bounded by :data:`_GIT_TIMEOUT`
  and folding a hung git into a synthetic FAILED result rather than a new exception type.
- the verbs (:func:`_git_add`, :func:`_git_commit`, :func:`_git_rm`,
  :func:`_git_commit_paths`, :func:`_restore_paths`, :func:`_unstage`) — each composing the
  SHARED retries from ``_store/gitutil.py``: index.lock contention + stale-lock reclaim
  (:func:`_with_index_lock_retry`) around the runner-FS transient fault retry
  (:func:`_with_transient_fault_retry`).

``gitutil`` owns the retry machinery, shared with the claim/transition path
(``_commands/txn.py``); this module is the event-commit path's own verb set built on top of
it. Callers import these names into ``event_append`` and call them by BARE NAME, so the
module-global lookup stays monkeypatch-visible for the store test suite.
"""

from __future__ import annotations

import os
import subprocess

from rebar._store.gitutil import (
    _is_transient_object_write_error,
    _with_index_lock_retry,
    _with_transient_fault_retry,
    run_git_bounded,
)

# The transient object-DB WRITE fault this path rides out — git's loose-object temp create
# blipping on a CI-runner FS during ``git add``/``git commit`` — is classified and retried by
# the SHARED seam (``gitutil._TRANSIENT_WRITE_MARKERS`` /
# :func:`gitutil._with_transient_fault_retry`).
# The marker family used to live here privately, which left every OTHER caller of the shared
# write seam unprotected against the identical fault (bug unheedful-custodial-bluebottle); it
# now has exactly one definition, in gitutil. Re-exported under this module's historical name
# so a test that pins the classification keeps a symbol to assert on.
_is_transient_add_error = _is_transient_object_write_error
_GIT_ADD_ATTEMPTS = 3


# git's index.lock self-healing (constants + ``_is_index_lock_error`` +
# ``_reclaim_if_stale_index_lock`` + ``_with_index_lock_retry``) now lives in the SHARED
# ``rebar._store.gitutil`` so the claim/transition write path (txn.py) self-heals through the
# same implementation (bug fix-indexlock-retry). Imported at module top; ``_INDEX_LOCK_STALE_S``
# is re-exported there for tests. ``_git_add`` below composes gitutil's index.lock retry with
# gitutil's runner-FS transient retry (:func:`_with_transient_fault_retry`).


# Bound every lock-held git child (c2ba). These run INSIDE ``lock.write_lock`` holding the
# store's MKDIR write lock, so a stuck/contended tracker volume would otherwise hold that lock
# indefinitely — the residue that made the review-bot ``stop_grace_period`` unprovable and, on a
# SIGKILL mid-write, orphaned the lock (the 2026-07-31 autodeploy incident). ``_store/push.py``
# already bounds its git calls with the SAME ``_GIT_TIMEOUT``; this closes the inconsistency.
_GIT_TIMEOUT = 30


# raw-git-ok: locked store seam internal
def _run_git(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` a git command (captured, text) bounded by :data:`_GIT_TIMEOUT`.

    The single entry point for event_append's lock-held git invocations, so every one carries
    the same wall-clock bound as ``push.py``. The timeout fold — a hung git becomes a
    synthetic FAILED result (returncode 124) rather than a raise, so the existing
    returncode-inspecting callers and their retry wrappers fail the write cleanly, unwinding
    out of ``write_lock`` — is now the SHARED :func:`gitutil.run_git_bounded`; this shim only
    adapts the historical ``argv``-list signature its ~15 call sites pass. A genuine
    ``OSError`` (e.g. git not on PATH) still propagates unchanged, preserving the best-effort
    helpers' ``except OSError`` behavior."""
    if argv[:2] == ["git", "-C"]:
        return run_git_bounded(argv[2], *argv[3:], timeout=_GIT_TIMEOUT)
    return run_git_bounded(None, *argv[1:], timeout=_GIT_TIMEOUT)


# raw-git-ok: locked store seam internal
def _git_add(
    tracker: str, relpaths: list[str], *, attempts: int = _GIT_ADD_ATTEMPTS
) -> subprocess.CompletedProcess[str]:
    """``git -C tracker add -- <relpaths>``, retrying transient object-DB AND index.lock
    failures.

    On success or a NON-transient failure returns immediately (behavior unchanged — a
    real pathspec/permission/UU error still surfaces on the first attempt). On a transient
    runner-FS signature the identical add is retried up to *attempts* times with a short
    backoff by the shared :func:`_with_transient_fault_retry`, because re-adding the same
    paths is idempotent and the fault clears on retry; index.lock contention is ridden out
    (and a stale lock reclaimed) by :func:`_with_index_lock_retry`. Returns the final
    :class:`subprocess.CompletedProcess`."""

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
    """``git -C tracker commit -q --no-verify -m <msg>``, riding out two transients:
    index.lock contention (and reclaiming a stale lock) via :func:`_with_index_lock_retry`,
    and the runner-FS git faults via :func:`_with_transient_fault_retry` — the
    ``could not parse HEAD`` READ fault (``git commit`` parses HEAD to set the new commit's
    parent) and the object-DB temp-create WRITE fault (``git commit`` also WRITES the new
    tree + commit loose objects — the same blip that strikes ``git add``, whose unretried
    commit once dropped a concurrent locked write). Composed index.lock-OUTER /
    runner-FS-INNER — the same gitutil retries the transition/claim path uses. A
    non-lock, non-transient failure (including a genuine "nothing to commit" / UU wedge)
    surfaces immediately, unchanged — the caller's UU-recovery path still handles it."""
    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_fault_retry(
            lambda: _run_git(
                ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg]
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
    git faults via the same composed gitutil retries as :func:`_git_commit`."""
    argv = ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg, "--", *relpaths]
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
