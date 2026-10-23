"""Reconverge ticket-store clones without losing local commits.

Fetch outside the write lock; adopt or merge under it. Empty unrelated stores may
union, but unrelated stores with local events refuse and report ``DIVERGED``.
Related stores fast-forward, stay ahead, or merge as needed. Conflicts abort and
keep local state. Union merges keep both parents reachable for serialized gc;
auto-gc stays foreground under the store lock. The caller owns throttling, and
this best-effort operation returns ``None``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from rebar._store import compat, git_outcome, merge_recovery
from rebar._store import lock as _lock
from rebar._store.gitutil import (
    _with_transient_fault_retry,
    fetch_coordination_lock,
    run_git,
    run_git_bounded,
)

logger = logging.getLogger(__name__)

_SYNC_LOCK_TIMEOUT = 15  # bash TICKET_SYNC_LOCK_TIMEOUT default
# Bound git calls (notably the network `fetch`) so a stuck remote can't hang a
# sync indefinitely. These calls are best-effort already (`_ok` returns False on
# failure), so a timeout surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30

# The fetch lock serializes rebar peers; bounded retries cover CAS races with
# other Git peers. Exhaustion skips this best-effort freshness round.
_FETCH_CAS_ATTEMPTS = 3
_FETCH_CAS_BACKOFF_S = 0.1


# raw-git-ok: locked store seam internal
def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    """This module's bounded git seam; the timeout fold is the shared
    :func:`gitutil.run_git_bounded`, handed THIS module's ``run_git`` so the tests that
    patch ``sync.run_git`` by name keep intercepting every sync git call."""
    return run_git_bounded(tracker, *args, timeout=_GIT_TIMEOUT, runner=run_git)


# raw-git-ok: locked store seam internal
def _ok(tracker: str, *args: str) -> bool:
    return _git(tracker, *args).returncode == 0


def _coordinated_fetch(tracker: str, remote_name: str, refspec: str) -> bool:
    """Fetch under the shared lock, retrying residual ref-CAS races finitely.

    Return ``False`` on failure or exhaustion so sync skips this round.
    """
    for attempt in range(1, _FETCH_CAS_ATTEMPTS + 1):
        with fetch_coordination_lock(tracker):
            proc = _git(tracker, "fetch", remote_name, refspec, "--quiet")
        if proc.returncode == 0:
            return True
        combined = (proc.stderr or "") + (proc.stdout or "")
        if not git_outcome.is_ref_cas_mismatch(combined) or attempt == _FETCH_CAS_ATTEMPTS:
            return False
        time.sleep(_FETCH_CAS_BACKOFF_S)
    return False


def _carries_ticket_events(tracker: str) -> bool:
    """Detect non-dot ticket entries in HEAD's root tree.

    Unborn HEAD is empty; an unreadable tree with resolvable HEAD is treated
    as nonempty so unrelated-history recovery refuses rather than risks data.
    """
    if not _ok(tracker, "rev-parse", "--verify", "HEAD"):
        return False  # unborn HEAD: no commits, nothing at risk
    tree = _git(tracker, "ls-tree", "--name-only", "HEAD")
    if tree.returncode != 0:
        return True  # HEAD exists but is unreadable — refuse rather than guess
    return any(line.strip() and not line.startswith(".") for line in tree.stdout.splitlines())


# raw-git-ok: locked store seam internal
def _do_reconverge(tracker: str, branch: str, remote_name: str) -> None:
    """The locked mutation critical section (lock held, fetch already ran)."""
    remote = f"{remote_name}/{branch}"
    # Recovery guard, re-checked under the lock (637b): a reset/merge through an
    # interrupted rebase/merge would strand picks / clear MERGE_HEAD.
    try:
        _lock.check_no_rebase_in_progress(tracker)
    except _lock.RebaseGuard:
        logger.warning(
            "tickets sync skipped — tracker in rebase/merge recovery state "
            "(run: rebar store fsck-recover)"
        )
        return

    if not _ok(tracker, "rev-parse", "--verify", remote):
        return

    # Union an empty unrelated local store. Refuse one with ticket events:
    # independently built histories need operator recovery, not implicit grafting.
    if not _ok(tracker, "merge-base", "HEAD", remote):
        if _carries_ticket_events(tracker):
            logger.warning(
                "tickets sync DIVERGED — local store and %s share no common ancestor "
                "and the local store already carries ticket events; refusing to merge. "
                "Local state is untouched; run: rebar store fsck (then rebar store fsck-recover)",
                remote,
            )
            return
        _union_merge(tracker, remote, "--allow-unrelated-histories")
        return

    # Related histories. Local-ahead measured by HEAD (the WS3 fix).
    local_ahead = _git(tracker, "rev-list", f"{remote}..HEAD").stdout.strip()
    if not local_ahead:
        _git(tracker, "reset", "--hard", remote, "--quiet")  # ff-adopt
        return

    # Local strictly ahead (origin is an ancestor of HEAD) → nothing to merge.
    if _ok(tracker, "merge-base", "--is-ancestor", remote, "HEAD"):
        return

    # Diverged → merge-as-union. Conflict → abort, keep local, hint fsck.
    _union_merge(tracker, remote)


# raw-git-ok: locked store seam internal
def _union_merge(tracker: str, remote: str, *extra: str) -> None:
    """Merge both histories without orphaning commits; abort conflicts and keep local.

    ``extra`` permits the safe empty-store unrelated-history case.
    """
    merge_target, problem = compat.store_epoch_merge_target(tracker, remote)
    if merge_target is None or problem is not None:
        logger.warning("%s", problem or "tickets store epoch guard could not pin remote ref")
        return

    def _merge_once() -> subprocess.CompletedProcess:
        return _git(
            tracker,
            "merge",
            *extra,
            merge_target,
            "--no-edit",
            "-m",
            f"Merge {remote} (auto-reconcile during sync)",
        )

    # Retry transient pre-write object or filesystem faults through the shared seam.
    merge = _with_transient_fault_retry(_merge_once)
    if merge.returncode == 0:
        return

    # Recover only Git-named untracked collisions or local tracked changes:
    # quarantine leftovers, restore tracked artifacts, then retry once.
    # Other failures keep the abort-only path.
    if _recover_merge_abort(tracker, merge):
        retry = _with_transient_fault_retry(_merge_once)
        if retry.returncode == 0:
            return

    _git(tracker, "merge", "--abort")
    logger.warning(
        "tickets sync could not auto-merge %s — local state kept; run: rebar store fsck-recover",
        remote,
    )


def _recover_merge_abort(tracker: str, merge: subprocess.CompletedProcess) -> bool:
    """Recover every Git-named abort class before permitting one merge retry.

    Unknown or partly recovered errors retain the abort-only path.
    """
    leftovers = merge_recovery.untracked_overwrite_paths(merge)
    local_changes = merge_recovery.local_change_paths(merge)
    if not leftovers and not local_changes:
        return False
    _git(tracker, "merge", "--abort")
    if leftovers and not _quarantine_untracked(tracker, leftovers):
        return False
    return not local_changes or _restore_local_changes(tracker, local_changes)


def _quarantine_untracked(tracker: str, paths: list[str]) -> bool:
    """Quarantine only named untracked paths through this module's patchable git seam.

    Doctor and sync share the mover's ``??`` fence.
    """
    return merge_recovery.quarantine_untracked(_git, tracker, paths)


def _restore_local_changes(tracker: str, paths: list[str]) -> bool:
    """Restore the tracked files git named as locally changed. Adapter over the shared
    :func:`merge_recovery.restore_local_changes`, on this module's ``_git`` seam."""
    return merge_recovery.restore_local_changes(_git, tracker, paths)


def reconverge(tracker: str | os.PathLike, *, lock_timeout: int = _SYNC_LOCK_TIMEOUT) -> None:
    """Reconverge best-effort under the write lock, without throttling here.

    Readers can pass a shorter timeout and keep their local snapshot on contention;
    writers use the default wait.
    """
    if not os.path.isdir(str(tracker)):
        return
    tracker = _lock.canonical_tracker(tracker)

    # Resolve store branch and remote from the tracker's parent; malformed
    # config skips this best-effort sync.
    from rebar.config import ConfigError, tickets_branch, tickets_remote

    try:
        # repo-root-ok: branch/remote of the STORE's own git repo (git -C tracker), not code config
        branch = tickets_branch(os.path.dirname(str(tracker)))
        # repo-root-ok: branch/remote of the STORE's own git repo (git -C tracker), not code config
        remote_name = tickets_remote(os.path.dirname(str(tracker)))
    except ConfigError:
        return

    # Fetch outside the lock with an explicit destination refspec. A bare fetch
    # can exit 0 without updating origin/tickets in a single-branch clone.
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote_name}/{branch}"
    if not _coordinated_fetch(tracker, remote_name, refspec):
        return
    # Still guard on the ref existing: a remote that genuinely has no such branch
    # fetches fine (nothing matched) and must remain a quiet no-op.
    if not _ok(tracker, "rev-parse", "--verify", f"{remote_name}/{branch}"):
        return

    # Classify recovery state only after taking the lock: an in-flight merge may
    # temporarily leave MERGE_HEAD, while a marker that survives lock acquisition
    # indicates stranded recovery. Contention skips this round.
    try:
        with _lock.write_lock(tracker, timeout=lock_timeout, attempts=1, dual_window=True):
            _do_reconverge(tracker, branch, remote_name)
    except _lock.LockTimeout:
        return
