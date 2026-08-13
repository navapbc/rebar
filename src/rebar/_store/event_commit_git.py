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
  SHARED retries from ``_store/gitutil.py`` (index.lock contention + stale-lock reclaim,
  the transient ``could not parse HEAD`` read fault) with this module's own object-DB
  temp-create WRITE retry (:func:`_with_transient_add_retry`).

``gitutil`` is the machinery shared with the claim/transition path (``_commands/txn.py``);
this module is the event-commit path's own verb set built on top of it. Callers import
these names into ``event_append`` and call them by BARE NAME, so the module-global lookup
stays monkeypatch-visible for the store test suite.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

from rebar._store.gitutil import (
    _with_index_lock_retry,
    _with_transient_head_retry,
)

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


def _with_transient_add_retry(
    run_once: Callable[[], subprocess.CompletedProcess[str]],
    *,
    attempts: int = _GIT_ADD_ATTEMPTS,
) -> subprocess.CompletedProcess[str]:
    """Retry a loose-object-WRITING git invocation on the transient object-DB temp-create
    signature (:func:`_is_transient_add_error`).

    BOTH ``git add`` (a blob) and ``git commit`` (a tree + a commit object) write loose
    objects through git's identical ``create_tmpfile`` path, which intermittently blips on a
    CI-runner FS. Re-running the same add/commit re-writes the same objects (idempotent) and
    the fault clears, so this used to self-heal ``git add`` only — leaving ``git commit`` to
    surface the SAME transient as a hard write loss, dropping a concurrent locked write (the
    enrich-prune concurrency flake). A non-transient failure returns immediately, unchanged."""
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        result = run_once()
        if result.returncode == 0:
            return result
        if attempt < attempts and _is_transient_add_error(result.stderr or result.stdout or ""):
            time.sleep(_GIT_ADD_BACKOFF_S * attempt)
            continue
        return result
    assert result is not None  # attempts >= 1, so the loop body ran at least once
    return result


# git's index.lock self-healing (constants + ``_is_index_lock_error`` +
# ``_reclaim_if_stale_index_lock`` + ``_with_index_lock_retry``) now lives in the SHARED
# ``rebar._store.gitutil`` so the claim/transition write path (txn.py) self-heals through the
# same implementation (bug fix-indexlock-retry). Imported at module top; ``_INDEX_LOCK_STALE_S``
# is re-exported from ``event_append`` for tests. ``_git_add`` below composes gitutil's
# index.lock retry with THIS module's object-DB ``git add`` retry (the
# ``_TRANSIENT_ADD_MARKERS`` loop above).


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
    the same wall-clock bound as ``push.py``. Mirrors ``push.py._git``: a hung git is folded
    into a synthetic FAILED result (returncode 124) rather than raised, so the existing
    returncode-inspecting callers (and their retry wrappers) fail the write cleanly — which
    unwinds out of ``write_lock`` and releases the lock — instead of surfacing a new
    ``TimeoutExpired`` exception type. A genuine ``OSError`` (e.g. git not on PATH) still
    propagates unchanged, preserving the best-effort helpers' ``except OSError`` behavior."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git timed out after {_GIT_TIMEOUT}s")


# raw-git-ok: locked store seam internal
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

    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_add_retry(
            lambda: _run_git(["git", "-C", tracker, "add", "--", *relpaths]),
            attempts=attempts,
        ),
        force_reclaim=True,
    )


# raw-git-ok: locked store seam internal
def _git_commit(tracker: str, commit_msg: str) -> subprocess.CompletedProcess[str]:
    """``git -C tracker commit -q --no-verify -m <msg>``, riding out three transients:
    index.lock contention (and reclaiming a stale lock) via :func:`_with_index_lock_retry`;
    the ``could not parse HEAD`` READ fault via :func:`_with_transient_head_retry` (``git
    commit`` parses HEAD to set the new commit's parent); and the object-DB temp-create WRITE
    fault via :func:`_with_transient_add_retry` (``git commit`` also WRITES the new tree +
    commit loose objects — the same runner-FS blip that strikes ``git add``, previously
    unretried on commit, which dropped a concurrent locked write). Composed index.lock-OUTER /
    HEAD-parse / object-DB-INNER — the same gitutil retries the transition/claim path uses. A
    non-lock, non-transient failure (including a genuine "nothing to commit" / UU wedge)
    surfaces immediately, unchanged — the caller's UU-recovery path still handles it."""
    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_head_retry(
            lambda: _with_transient_add_retry(
                lambda: _run_git(
                    ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg]
                )
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
    the write lock's braces. Rides out index.lock contention AND the transient
    ``could not parse HEAD`` read fault via the same composed gitutil retries as
    :func:`_git_commit`."""
    argv = ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", commit_msg, "--", *relpaths]
    return _with_index_lock_retry(
        tracker,
        lambda: _with_transient_head_retry(
            lambda: _with_transient_add_retry(lambda: _run_git(argv))
        ),
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
