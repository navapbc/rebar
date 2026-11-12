"""Reconverge ticket-store clones without losing local commits.

Fetch outside the write lock; adopt or merge under it. Empty unrelated stores may
union, but unrelated stores with local events refuse and report ``DIVERGED``.
Related stores fast-forward, stay ahead, or merge as needed. Conflicts abort and
keep local state. Union merges keep both parents reachable, so the write-locked
maintenance step never loses a ticket commit. The caller owns throttling, and
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
    _LOCAL_GIT_TIMEOUT,
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
# These subcommands refresh or rewrite the whole index, so their cost grows with the
# store, not with the link. They take gitutil's local bound instead of the network one
# (bug painted-epidermal-shelduck: a 213k-entry store's merge outran 30 s).
_INDEX_SUBCOMMANDS = frozenset({"merge", "reset", "checkout", "status"})

# The fetch lock serializes rebar peers; bounded retries cover CAS races with
# other Git peers. Exhaustion skips this best-effort freshness round.
_FETCH_CAS_ATTEMPTS = 3
_FETCH_CAS_BACKOFF_S = 0.1


# raw-git-ok: locked store seam internal
def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    """This module's bounded git seam; the timeout fold is the shared
    :func:`gitutil.run_git_bounded`, handed THIS module's ``run_git`` so the tests that
    patch ``sync.run_git`` by name keep intercepting every sync git call. Index-wide
    subcommands take the local bound; everything else keeps the network bound."""
    index_wide = bool(args) and args[0] in _INDEX_SUBCOMMANDS
    bound = _LOCAL_GIT_TIMEOUT if index_wide else _GIT_TIMEOUT
    return run_git_bounded(tracker, *args, timeout=bound, runner=run_git)


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
    _warn_merge_failed(remote, merge)


def _warn_merge_failed(remote: str, merge: subprocess.CompletedProcess) -> None:
    """Report an aborted merge as what it was. A watchdog kill is not a conflict, and
    ``fsck-recover`` cannot fix a merge that only needs more time."""
    if git_outcome.is_watchdog_timeout(merge):
        logger.warning(
            "tickets sync: git merge of %s timed out after %ss and was aborted — local "
            "state kept. This is not a merge conflict; the next sync tries again.",
            remote,
            _LOCAL_GIT_TIMEOUT,
        )
        return
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
    _reconverge(tracker, lock_timeout)


def _store_destination(tracker: str) -> tuple[str, str] | None:
    """``(branch, remote)`` of the STORE, or ``None`` on malformed config.

    Read from the tracker's parent, only those 2 keys, so the rest of that checkout's
    config is not reported against a linked-worktree caller."""
    from rebar.config import ConfigError, store_tickets_branch, store_tickets_remote

    try:
        # repo-root-ok: branch/remote of the STORE's own git repo (git -C tracker), not code config
        branch = store_tickets_branch(os.path.dirname(tracker))
        # repo-root-ok: branch/remote of the STORE's own git repo (git -C tracker), not code config
        remote_name = store_tickets_remote(os.path.dirname(tracker))
    except ConfigError:
        return None
    return branch, remote_name


def _reconverge(tracker: str | os.PathLike, lock_timeout: int) -> str:
    """:func:`reconverge`, returning how the round ended: ``done``, ``lock-busy``, or a
    skip (``absent``, ``no-config``, ``fetch-failed``, ``no-remote-branch``)."""
    if not os.path.isdir(str(tracker)):
        return "absent"
    tracker = _lock.canonical_tracker(tracker)
    destination = _store_destination(str(tracker))
    if destination is None:
        return "no-config"
    branch, remote_name = destination

    # Fetch outside the lock with an explicit destination refspec. A bare fetch
    # can exit 0 without updating origin/tickets in a single-branch clone.
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote_name}/{branch}"
    if not _coordinated_fetch(tracker, remote_name, refspec):
        return "fetch-failed"
    # Still guard on the ref existing: a remote that genuinely has no such branch
    # fetches fine (nothing matched) and must remain a quiet no-op.
    if not _ok(tracker, "rev-parse", "--verify", f"{remote_name}/{branch}"):
        return "no-remote-branch"

    # Classify recovery state only after taking the lock: an in-flight merge may
    # temporarily leave MERGE_HEAD, while a marker that survives lock acquisition
    # indicates stranded recovery. Contention skips this round.
    try:
        with _lock.write_lock(tracker, timeout=lock_timeout, attempts=1, dual_window=True):
            _do_reconverge(tracker, branch, remote_name)
    except _lock.LockTimeout:
        return "lock-busy"
    return "done"


# The idle sync's lock budget: one short pass, because the next tick retries.
_IDLE_LOCK_TIMEOUT = 5


def idle_sync(tracker: str | os.PathLike, *, lock_timeout: int = _IDLE_LOCK_TIMEOUT) -> str:
    """One idle-timer round: reconverge, publish any backlog, and log the outcome.

    Without it a fork is only pushed when a client writes (bug topnotch-brazen-slug). Every
    outcome an operator must act on, or that ends an outage, is logged at WARNING, the
    level a server's stderr handler shows by default: ``pending`` (commits still
    undelivered), ``diverged`` (no common history with the shared branch), ``lock-busy``
    (with the holder), ``delivered`` (a recorded backlog published). ``current`` and the
    skips stay at DEBUG so a healthy server is quiet. Returns the outcome."""
    from rebar._store import push, push_state

    status = _reconverge(tracker, lock_timeout)
    if status == "lock-busy":
        logger.warning(
            "tickets idle sync skipped: store write lock busy (holder: %s); retrying next tick",
            _lock.describe_lock_holder(str(tracker)),
        )
        return status
    if status in ("absent", "no-config", "no-remote-branch"):
        logger.debug("tickets idle sync skipped: %s", status)
        return status
    tracker = _lock.canonical_tracker(tracker)
    destination = _store_destination(tracker)
    if destination is None:  # config changed under us since _reconverge read it
        logger.debug("tickets idle sync skipped: no-config")
        return "no-config"
    remote_ref = f"{destination[1]}/{destination[0]}"
    if _ok(tracker, "rev-parse", "--verify", remote_ref) and not _ok(
        tracker, "merge-base", "HEAD", remote_ref
    ):
        # reconverge refused to graft the unrelated history; pushing would only be rejected.
        logger.warning(
            "tickets idle sync: DIVERGED — the local store shares no history with %s; "
            "nothing can be delivered (run: rebar store fsck)",
            remote_ref,
        )
        return "diverged"
    was_pending = push_state.read_status(tracker).get("state") == "pending"
    # An unmeasurable backlog ("unknown") is pushed too: a no-op push is cheap, and
    # skipping it would let a real backlog read as "current".
    if was_pending or push_state.unpushed_count(tracker, remote_ref) != "0":
        push.push_after_commit(tracker)
    return _report_idle_outcome(tracker, remote_ref, was_pending)


def _report_idle_outcome(tracker: str, remote_ref: str, was_pending: bool) -> str:
    from rebar._store import push_state

    marker = push_state.read_status(tracker)
    if marker.get("state") == "pending":
        logger.warning(
            "tickets idle sync: PUSH_PENDING (%s) — %s commit(s) not on %s; retrying next tick: %s",
            marker.get("reason", "unknown"),
            push_state.unpushed_count(tracker, remote_ref),
            remote_ref,
            (str(marker.get("detail") or "").strip().splitlines() or [""])[-1],
        )
        return "pending"
    if was_pending:
        logger.warning("tickets idle sync: delivered — the pending backlog reached %s", remote_ref)
        return "delivered"
    logger.debug("tickets idle sync: current with %s", remote_ref)
    return "current"
