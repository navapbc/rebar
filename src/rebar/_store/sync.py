"""Cross-clone reconvergence of the tickets branch.

Faithful port of ``_reconverge_tickets`` / ``_do_reconverge_tickets``
(ticket-sync.sh). Best-effort: fetch happens OUTSIDE the write lock (it only moves
remote-tracking refs, never HEAD/index/worktree, so it can't race a local
committer and a slow fetch must not block writers); the reset/merge that mutate
HEAD run UNDER the unified write lock.

**Recovery is non-destructive — the safety invariant (epic 97e7 / P1.4).** Both
the unrelated- and diverged-history paths reconverge by ``git merge`` (a UNION
that keeps both parents), never by a reset that orphans local commits. So:

    after reconverge, every commit rebar cares about is reachable from the
    ``tickets`` ref ⇒ stock ``git gc`` is safe by construction (it can only ever
    collect truly unreachable objects).

This is why rebar no longer forces ``gc.auto=0`` (see the ``gc-config`` ensure unit
``init._gc_config_unit``, run via ``rebar._store.ensures.run_ensures``)
and why the reflog is no longer load-bearing. UUID-named event files never collide
on merge; the only shared mutable root file (``.bridge_state/*``) resolves via the
tickets-branch ``.gitattributes`` ``merge=ours`` (it is per-pass
derived caches the reconciler rebuilds). Resolution by case: unrelated histories →
``merge --allow-unrelated-histories`` (union); related + no local commits →
fast-forward adopt (``reset --hard`` onto an ancestor — discards nothing); local
strictly ahead → nothing to do; diverged → ``merge origin/tickets`` (union). Every
merge: on conflict → ``merge --abort``, keep local, hint ``fsck`` (never reset,
never hard-fail a read).

The ≤1/min throttle + ``/tmp/.ticket-sync-<md5>`` marker live in the CALLER
(``reads.py::ensure_fresh``), NOT here — this function is throttle-free, matching
bash. Always returns ``None``.
"""

from __future__ import annotations

import logging
import os
import subprocess

from rebar._store import lock as _lock
from rebar._store.gitutil import run_git

logger = logging.getLogger(__name__)

_SYNC_LOCK_TIMEOUT = 15  # bash TICKET_SYNC_LOCK_TIMEOUT default
# Bound git calls (notably the network `fetch`) so a stuck remote can't hang a
# sync indefinitely. These calls are best-effort already (`_ok` returns False on
# failure), so a timeout surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30


def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return run_git(tracker, *args, check=False, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", tracker, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


def _ok(tracker: str, *args: str) -> bool:
    return _git(tracker, *args).returncode == 0


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
            "(run: rebar fsck-recover)"
        )
        return

    if not _ok(tracker, "rev-parse", "--verify", remote):
        return

    # Unrelated histories (no common ancestor): UNION them, never discard local.
    # The append-only event files are UUID-named so they never collide; the only
    # shared mutable root file (.bridge_state/*) resolves via the tickets-branch
    # .gitattributes `merge=ours` (WU-3). Reuses the diverged-path
    # conflict net below (abort → keep local → hint fsck) — extend, don't reinvent.
    if not _ok(tracker, "merge-base", "HEAD", remote):
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


def _union_merge(tracker: str, remote: str, *extra: str) -> None:
    """Merge ``origin/<branch>`` into HEAD as a union — both parents are kept, so no
    local commit is ever orphaned (this is what lets stock ``git gc`` be safe; the
    reflog is no longer load-bearing). ``extra`` carries ``--allow-unrelated-histories``
    for the no-common-ancestor case. On the rare genuine conflict: abort, keep
    local, hint fsck — never discard local commits."""
    merge = _git(
        tracker,
        "merge",
        *extra,
        remote,
        "--no-edit",
        "-m",
        f"Merge {remote} (auto-reconcile during sync)",
    )
    if merge.returncode != 0:
        _git(tracker, "merge", "--abort")
        logger.warning(
            "tickets sync could not auto-merge %s — local state kept; run: rebar fsck-recover",
            remote,
        )


def reconverge(tracker: str | os.PathLike, *, lock_timeout: int = _SYNC_LOCK_TIMEOUT) -> None:
    """Acquire the write lock, then reconverge (best-effort). No throttle here.

    ``lock_timeout`` bounds how long to wait for the write lock before skipping this
    round. The read path passes a SHORT value (bug slim-fetch-ledge): reconverge is
    a freshness optimization, so a read must prefer its consistent local snapshot
    over stalling many seconds while a concurrent background push holds the lock.
    Writers keep the default (a sync is still best-effort, but they tolerate a
    longer wait)."""
    if not os.path.isdir(str(tracker)):
        return
    tracker = _lock.canonical_tracker(tracker)

    # Cheap pre-lock early-out: skip a tracker mid rebase/merge recovery.
    try:
        _lock.check_no_rebase_in_progress(tracker)
    except _lock.RebaseGuard:
        logger.warning(
            "tickets sync skipped — tracker in rebase/merge recovery state "
            "(run: rebar fsck-recover)"
        )
        return

    # Branch + remote resolved from the MAIN repo config (the tracker's parent), matching
    # reads._sync_disabled / _push_mode. Best-effort: a malformed config skips sync.
    from rebar.config import ConfigError, tickets_branch, tickets_remote

    try:
        branch = tickets_branch(os.path.dirname(str(tracker)))
        remote_name = tickets_remote(os.path.dirname(str(tracker)))
    except ConfigError:
        return

    # Fetch OUTSIDE the lock (only moves remote-tracking refs).
    if not _ok(tracker, "fetch", remote_name, branch, "--quiet"):
        return
    if not _ok(tracker, "rev-parse", "--verify", f"{remote_name}/{branch}"):
        return

    # Locked reset/merge. Best-effort on lock contention (another writer/syncer holds
    # it) — bash does `flock -w 15 || exit 0`, so a timeout silently skips this round.
    try:
        with _lock.write_lock(tracker, timeout=lock_timeout, attempts=1, dual_window=True):
            _do_reconverge(tracker, branch, remote_name)
    except _lock.LockTimeout:
        return
