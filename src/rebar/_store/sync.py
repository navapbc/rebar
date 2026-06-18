"""Cross-clone reconvergence of the tickets branch (Tier D, ``REBAR_WRITE_CORE``).

Faithful port of ``_reconverge_tickets`` / ``_do_reconverge_tickets``
(ticket-sync.sh). Best-effort: fetch happens OUTSIDE the write lock (it only moves
remote-tracking refs, never HEAD/index/worktree, so it can't race a local
committer and a slow fetch must not block writers); the reset/merge that mutate
HEAD run UNDER the unified write lock. Resolution: unrelated histories →
``merge --allow-unrelated-histories`` as a union (never discards local commits);
related + no local commits → fast-forward adopt; local strictly ahead → nothing
to do; diverged → ``merge origin/tickets`` as a union (conflict → abort, keep
local, hint fsck).

The ≤1/min throttle + ``/tmp/.ticket-sync-<md5>`` marker live in the CALLER
(``reads.py::ensure_fresh``), NOT here — this function is throttle-free, matching
bash. Always returns ``None``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from rebar._store import lock as _lock

_SYNC_LOCK_TIMEOUT = 15  # bash TICKET_SYNC_LOCK_TIMEOUT default
# Bound git calls (notably the network `fetch`) so a stuck remote can't hang a
# sync indefinitely. These calls are best-effort already (`_ok` returns False on
# failure), so a timeout surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30


def _git(tracker: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", tracker, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", tracker, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


def _ok(tracker: str, *args: str) -> bool:
    return _git(tracker, *args).returncode == 0


def _do_reconverge(tracker: str) -> None:
    """The locked mutation critical section (lock held, fetch already ran)."""
    # Recovery guard, re-checked under the lock (637b): a reset/merge through an
    # interrupted rebase/merge would strand picks / clear MERGE_HEAD.
    try:
        _lock.check_no_rebase_in_progress(tracker)
    except _lock.RebaseGuard:
        print(
            "Warning: tickets sync skipped — tracker in rebase/merge recovery state "
            "(run: rebar fsck-recover)",
            file=sys.stderr,
        )
        return

    if not _ok(tracker, "rev-parse", "--verify", "origin/tickets"):
        return

    # Unrelated histories (no common ancestor): UNION them, never discard local.
    # The append-only event files are UUID-named so they never collide; the only
    # shared mutable root files (.bridge_state/*, .reconciler-*) resolve via the
    # tickets-branch .gitattributes `merge=ours` (WU-3). Reuses the diverged-path
    # conflict net below (abort → keep local → hint fsck) — extend, don't reinvent.
    if not _ok(tracker, "merge-base", "HEAD", "origin/tickets"):
        _union_merge(tracker, "--allow-unrelated-histories")
        return

    # Related histories. Local-ahead measured by HEAD (the WS3 fix).
    local_ahead = _git(tracker, "rev-list", "origin/tickets..HEAD").stdout.strip()
    if not local_ahead:
        _git(tracker, "reset", "--hard", "origin/tickets", "--quiet")  # ff-adopt
        return

    # Local strictly ahead (origin is an ancestor of HEAD) → nothing to merge.
    if _ok(tracker, "merge-base", "--is-ancestor", "origin/tickets", "HEAD"):
        return

    # Diverged → merge-as-union. Conflict → abort, keep local, hint fsck.
    _union_merge(tracker)


def _union_merge(tracker: str, *extra: str) -> None:
    """Merge ``origin/tickets`` into HEAD as a union — both parents are kept, so no
    local commit is ever orphaned (this is what lets stock ``git gc`` be safe; the
    reflog is no longer load-bearing). ``extra`` carries ``--allow-unrelated-histories``
    for the no-common-ancestor case. On the rare genuine conflict: abort, keep
    local, hint fsck — never discard local commits."""
    merge = _git(
        tracker,
        "merge",
        *extra,
        "origin/tickets",
        "--no-edit",
        "-m",
        "Merge origin/tickets (auto-reconcile during sync)",
    )
    if merge.returncode != 0:
        _git(tracker, "merge", "--abort")
        print(
            "Warning: tickets sync could not auto-merge origin/tickets — local state "
            "kept; run: rebar fsck-recover",
            file=sys.stderr,
        )


def reconverge(tracker: str | os.PathLike) -> None:
    """Acquire the write lock, then reconverge (best-effort). No throttle here."""
    if not os.path.isdir(str(tracker)):
        return
    tracker = _lock.canonical_tracker(tracker)

    # Cheap pre-lock early-out: skip a tracker mid rebase/merge recovery.
    try:
        _lock.check_no_rebase_in_progress(tracker)
    except _lock.RebaseGuard:
        print(
            "Warning: tickets sync skipped — tracker in rebase/merge recovery state "
            "(run: rebar fsck-recover)",
            file=sys.stderr,
        )
        return

    # Fetch OUTSIDE the lock (only moves remote-tracking refs).
    if not _ok(tracker, "fetch", "origin", "tickets", "--quiet"):
        return
    if not _ok(tracker, "rev-parse", "--verify", "origin/tickets"):
        return

    # Locked reset/merge. Best-effort on lock contention (another writer/syncer holds
    # it) — bash does `flock -w 15 || exit 0`, so a timeout silently skips this round.
    try:
        with _lock.write_lock(tracker, timeout=_SYNC_LOCK_TIMEOUT, attempts=1, dual_window=True):
            _do_reconverge(tracker)
    except _lock.LockTimeout:
        return
