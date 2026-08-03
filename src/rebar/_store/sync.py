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
    ``tickets`` ref ⇒ a SERIAL ``git gc`` is safe by construction (it can only ever
    collect truly unreachable objects).

This reachability guarantee is why rebar no longer needs ``gc.auto=0`` to protect a
reflog-only recovery net (the reflog is no longer load-bearing). **It does NOT make a
CONCURRENT gc safe:** the tickets store is a linked worktree sharing the object DB, and
a *detached* background ``git gc`` / ``git maintenance run --auto`` repacks it OUTSIDE
the write lock, racing in-flight writers and corrupting the store (bug 88eb). So the
``gc-config`` ensure unit (``init._gc_config_unit``, run via
``rebar._store.ensures.run_ensures``) keeps auto-gc enabled but forces it FOREGROUND
(``gc.autoDetach=false`` + ``maintenance.autoDetach=false``) so it runs serialized under
the write lock — see ADR 0051. UUID-named event files never collide
on merge; the only shared mutable root file (``.bridge_state/*``) resolves via the
tickets-branch ``.gitattributes`` ``merge=ours`` (it is per-pass
derived caches the reconciler rebuilds). Resolution by case: unrelated histories
(no common ancestor) + an EMPTY local store → ``merge --allow-unrelated-histories``
(union — nothing local can be lost); unrelated histories + a local store that already
carries ticket events → **REFUSE**, mutate nothing, and log a ``DIVERGED`` warning
pointing at ``rebar fsck`` / ``rebar fsck-recover`` (see ``_carries_ticket_events``);
related + no local commits → fast-forward adopt (``reset --hard`` onto an ancestor —
discards nothing); local strictly ahead → nothing to do; diverged → ``merge
origin/tickets`` (union). Every merge: on conflict → ``merge --abort``, keep local,
hint ``fsck`` (never reset, never hard-fail a read).

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


# raw-git-ok: locked store seam internal
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


# raw-git-ok: locked store seam internal
def _ok(tracker: str, *args: str) -> bool:
    return _git(tracker, *args).returncode == 0


def _carries_ticket_events(tracker: str) -> bool:
    """Does this tracker's HEAD hold any ticket events at all?

    The tickets store has a strict root layout: every ticket is a directory named by
    its UUID, and every *non*-ticket root entry is dot-prefixed (``.gitignore``,
    ``.gitattributes``, ``.pre-commit-config.yaml``, ``.store-compat.json``,
    ``.bridge_state``, ``.ticket-write.lock``). So "carries ticket events" is exactly
    "``git ls-tree HEAD`` names at least one entry that does not start with a dot" —
    no walk of the tree, one cheap git call.

    Two different failures hide behind "``ls-tree`` did not work", and they must answer
    OPPOSITELY, because this predicate gates the destructive-if-wrong branch:

    * **Unborn HEAD** (a tracker with no commits at all) → False. There is provably
      nothing to lose, so the caller may safely adopt the shared branch.
    * **HEAD resolves but its tree is unreadable** → True, i.e. REFUSE. We cannot show
      the store is empty, and the whole point of this predicate is to fail toward
      reporting DIVERGED rather than toward absorbing a store we could not inspect.
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
            "(run: rebar fsck-recover)"
        )
        return

    if not _ok(tracker, "rev-parse", "--verify", remote):
        return

    # Unrelated histories (no common ancestor). Two sub-cases, split by whether the
    # local store has anything to lose (see `_carries_ticket_events`):
    #
    #   * EMPTY local store → UNION them. Nothing local can be lost, and this is the
    #     ordinary "fresh tracker adopts the shared branch" path. The append-only event
    #     files are UUID-named so they never collide; the only shared mutable root file
    #     (.bridge_state/*) resolves via the tickets-branch .gitattributes `merge=ours`
    #     (WU-3). Reuses the diverged-path conflict net below (abort → keep local →
    #     hint fsck) — extend, don't reinvent.
    #
    #   * NON-EMPTY local store → REFUSE, and mutate nothing. Two histories with no
    #     common ancestor but events on BOTH sides is not a routine drift; it means
    #     this clone's store and the remote's were built independently (a re-init, a
    #     restored backup, a wrong `sync.remote`). Silently union-merging that would
    #     graft an orphan store into the SHARED tickets branch for everyone, which is
    #     unreviewable after the fact. Sync is best-effort and runs implicitly on
    #     reads, so it is the wrong place to make that call: fail toward REPORTING
    #     (a DIVERGED warning naming the operator recovery path) rather than toward
    #     silently absorbing.
    if not _ok(tracker, "merge-base", "HEAD", remote):
        if _carries_ticket_events(tracker):
            logger.warning(
                "tickets sync DIVERGED — local store and %s share no common ancestor "
                "and the local store already carries ticket events; refusing to merge. "
                "Local state is untouched; run: rebar fsck (then rebar fsck-recover)",
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
    """Merge ``origin/<branch>`` into HEAD as a union — both parents are kept, so no
    local commit is ever orphaned (this is what lets a SERIAL ``git gc`` be safe; the
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
    #
    # The EXPLICIT refspec is load-bearing — do NOT "simplify" this back to a bare
    # `git fetch <remote> <branch>`. A bare fetch always writes FETCH_HEAD but only
    # *opportunistically* writes `refs/remotes/<remote>/<branch>`: it does so when the
    # remote's CONFIGURED refspec happens to cover that branch. A single-branch clone
    # configures `+refs/heads/main:refs/remotes/origin/main`, which does not cover
    # `tickets` — so the bare fetch exits 0 having created no `origin/tickets`, the
    # `rev-parse --verify` below fails with 128, and reconverge returns early. The
    # merge/adopt logic in `_do_reconverge` then never runs at all, silently. Naming
    # the destination ref makes the remote-tracking ref appear regardless of the
    # clone's configured refspec (same idiom as review_bot.gerrit_client).
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote_name}/{branch}"
    if not _ok(tracker, "fetch", remote_name, refspec, "--quiet"):
        return
    # Still guard on the ref existing: a remote that genuinely has no such branch
    # fetches fine (nothing matched) and must remain a quiet no-op.
    if not _ok(tracker, "rev-parse", "--verify", f"{remote_name}/{branch}"):
        return

    # Locked reset/merge. Best-effort on lock contention (another writer/syncer holds
    # it) — bash does `flock -w 15 || exit 0`, so a timeout silently skips this round.
    try:
        with _lock.write_lock(tracker, timeout=lock_timeout, attempts=1, dual_window=True):
            _do_reconverge(tracker, branch, remote_name)
    except _lock.LockTimeout:
        return
