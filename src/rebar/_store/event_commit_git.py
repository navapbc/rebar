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

import glob
import logging
import os
import subprocess
import time

from rebar._store.git_locking import _reclaim_if_stale, _resolve_common_git_dir
from rebar._store.gitutil import (
    _AUTOMAINT_OFF,
    _LOCAL_GIT_TIMEOUT,
    _with_index_lock_retry,
    _with_transient_fault_retry,
    run_git_bounded,
    run_git_reaped,
)

_GIT_ADD_ATTEMPTS = 3
_log = logging.getLogger(__name__)


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


# Incremental tasks never walk reachability: 0.35-17.3 s on the 1.07M-object prod store,
# where gc --auto took 134 s (bug 51e0-5038).
_MAINTENANCE_TASKS: tuple[str, ...] = (
    "--task=commit-graph",
    "--task=loose-objects",
    "--task=incremental-repack",
)


# Git never expires ``objects/maintenance.lock``; while it exists ``maintenance run`` exits 0
# doing nothing, so one hard kill disabled maintenance for good (bug 3e48-86e5). Git's own
# gc.pid lock goes stale after 12 h, far beyond any real run (full gc: 605.6 s on prod).
# Age, not rebar's write lock, proves staleness: a non-rebar git may share the object DB.
_MAINTENANCE_LOCK_STALE_S = 12 * 3600


def _reclaim_stale_maintenance_lock(tracker: str | os.PathLike[str]) -> None:
    """Remove a stale git maintenance lock left by a hard-killed run, and say so."""
    common = _resolve_common_git_dir(str(tracker))
    if not common:
        return
    # mechanism-ok: lock maintenance.lock — sulfuric-zebraic-hackee git's own lock, not rebar's
    lock_file = os.path.join(common, "objects", "maintenance.lock")
    if _reclaim_if_stale(lock_file, stale_s=_MAINTENANCE_LOCK_STALE_S):
        _log.warning("removed stale git maintenance lock %s (older than 12 h)", lock_file)


# Reflog expiry gets its own short bound, so the step's worst-case lock hold is the maintenance
# bound plus this. Measured 0.01-0.06 s on the production clone (bug successive-deceptive-xiphias).
_REFLOG_EXPIRE_TIMEOUT_S = 30.0
# Expiry runs at most once per interval: each git start costs 0.1-1.3 s on a loaded host, and
# git's shortest default expiry is 30 days, so a daily pass loses nothing. A stamp file's mtime
# in the common git dir records the last completed pass (or, on a store that has none yet, the
# first write, so the first pass runs a day later); the caller's write lock serializes it.
_REFLOG_EXPIRE_INTERVAL_S = 24 * 3600
_REFLOG_EXPIRE_STAMP = "rebar-reflog-expire.stamp"


def _reflog_expiry_due(common: str) -> bool:
    """True once a day. A store with no stamp starts the clock instead of expiring at once:
    a new store has nothing old enough to expire, and its first writes stay spawn-free."""
    try:
        last = os.path.getmtime(os.path.join(common, _REFLOG_EXPIRE_STAMP))
    except OSError:
        _stamp_reflog_expiry(common)
        return False
    return time.time() - last >= _REFLOG_EXPIRE_INTERVAL_S


def _stamp_reflog_expiry(common: str) -> None:
    stamp = os.path.join(common, _REFLOG_EXPIRE_STAMP)
    try:
        with open(stamp, "a", encoding="utf-8"):
            pass
        os.utime(stamp)
    except OSError as exc:
        _log.warning("could not record the reflog expiry pass at %s: %s", stamp, exc)


def _ref_locks(common: str) -> list[str]:
    """Every git ref or reflog lock under the common git dir: the HEAD and packed-refs locks,
    plus any lock file under ``refs/`` or ``logs/``. rebar's own lock files live elsewhere, so
    they are never reported."""
    git_locks = (
        # mechanism-ok: lock HEAD.lock — successive-deceptive-xiphias names git's lock only
        "HEAD.lock",
        # mechanism-ok: lock packed-refs.lock — successive-deceptive-xiphias names git's lock only
        "packed-refs.lock",
    )
    found = [path for name in git_locks if os.path.exists(path := os.path.join(common, name))]
    for sub in ("refs", "logs"):
        # mechanism-ok: lock *.lock — successive-deceptive-xiphias lists git's own ref locks only
        found += glob.glob(os.path.join(common, sub, "**", "*.lock"), recursive=True)
    return sorted(found)


def _expire_reflogs(tracker: str | os.PathLike[str]) -> None:
    """Expire reflog entries under git's configured policy (``gc.reflogExpire*``; no value here).

    ``git reflog expire`` rather than maintenance's ``reflog-expire`` task: that task needs git
    2.50, and the production container runs 2.47, where an unknown task fails the WHOLE
    maintenance run. Nothing else expires reflogs since bounded children run with gc off.
    """
    common = _resolve_common_git_dir(str(tracker)) or str(tracker)
    if not _reflog_expiry_due(common):
        return
    try:
        result = run_git_reaped(
            tracker, "reflog", "expire", "--all", timeout=_REFLOG_EXPIRE_TIMEOUT_S
        )
    except OSError as exc:
        _log.warning("git reflog expire could not start in %s: %s", tracker, exc)
        return
    if result.returncode == 124:
        # A killed expiry can strand a ref lock git never removes; rebar reclaims only
        # index.lock, so every later write would fail on it. Name what is there to remove.
        _log.error(
            "git reflog expire timed out in %s after %ss; remove any stranded ref lock it left "
            "(writes fail until then): %s",
            tracker,
            _REFLOG_EXPIRE_TIMEOUT_S,
            _ref_locks(common) or "none found",
        )
    elif result.returncode != 0:
        _log.warning(
            "git reflog expire failed in %s: rc=%s %s",
            tracker,
            result.returncode,
            (result.stderr or "").strip()[-300:],
        )
    else:
        _stamp_reflog_expiry(common)


# raw-git-ok: locked store seam internal
def run_auto_maintenance(
    tracker: str | os.PathLike[str], *, timeout: float = _LOCAL_GIT_TIMEOUT
) -> subprocess.CompletedProcess | None:
    """Run explicit best-effort Git maintenance after commit (bd66).

    The store's only maintenance: bounded Git children run with implicit maintenance off.
    The caller holds the write lock (ADR 0051); a timed-out group is reaped. Reflog expiry
    follows, skipped when maintenance timed out or could not launch so a slow step adds no
    more lock time. Unreachable objects are reclaimed only out of band
    (``infra/runbooks/tickets-store-prune.md``). A failure is logged, never raised. Return the
    maintenance result (timeout is ``124``), or ``None`` when Git cannot launch.
    """
    _reclaim_stale_maintenance_lock(tracker)
    try:
        result = run_git_reaped(
            tracker, "maintenance", "run", "--auto", "--quiet", *_MAINTENANCE_TASKS, timeout=timeout
        )
    except OSError as exc:
        _log.warning("git maintenance could not start in %s: %s", tracker, exc)
        return None
    if result.returncode != 124:
        _expire_reflogs(tracker)
    if result.returncode != 0:
        _log.warning(
            "git maintenance failed in %s: rc=%s %s",
            tracker,
            result.returncode,
            (result.stderr or "").strip()[-300:],
        )
    return result
