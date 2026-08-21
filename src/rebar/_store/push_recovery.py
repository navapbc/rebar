"""Push-recovery for the tickets branch: the stash/dirty-tree set-aside and the
non-fast-forward fetch-and-merge dance, extracted from :mod:`.push`.

These are the B (dirty working-tree recovery) and C (non-fast-forward recovery) subtrees
of :func:`push.push_tickets_branch`. They form one call-graph cluster whose only edge into
the rest of ``push`` is ``push_tickets_branch``'s single call to
:func:`_recover_non_fast_forward`; every other function here has exactly one caller inside
the cluster.

The ``core`` parameter
----------------------

Every function here takes the calling :mod:`rebar._store.push` **module object** as its
first argument and resolves ``core._git`` and ``core.logger`` through it at **call time**.
That is load-bearing, not ceremony: ``push._git`` is monkeypatched at ~25 sites across the
push test suite, and 8 of the 9 recovery functions shell out through it. A module-level
``from .push import _git`` here would BOTH create an import cycle AND bind the real ``_git``
before those patches are installed — the tests would keep asserting on a fake while this
code ran REAL git against the tracker. Late-binding through ``core`` keeps the patch point
working across the module boundary, exactly as
:mod:`rebar._engine.rebar_reconciler._ref_lock_push` does. ``logger`` is read from ``core``
too, so the operator-facing evidence stays on the ``rebar._store.push`` logger where it has
always been emitted (the store-epoch warning tests pin that logger name).

The dependency runs one way — ``push -> push_recovery`` — with no import cycle: ``push``
hands itself (and, for the write lock, the ``lock`` module) down, and this module imports
NOTHING from ``push``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from rebar._store import compat
from rebar._store.gitutil import discard_unmerged_paths, path_is_foreign_to_branch
from rebar._store.push_classify import (
    _DIRTY_WD,
    _MAX_TRANSPORT_ATTEMPTS,
    _cas_backoff,
    _is_transport_retriable,
    _raise_if_strict,
    _transport_backoff,
)

# The untracked-overwrite recovery is PARITY with sync.py's reconverge (variant (a),
# loris/1757, generalized by wolverine/1767): the PARSER and the quarantine PATH
# arithmetic are pure (no subprocess), so they are shared from sync; the MOVER is
# deliberately NOT — sync's `_quarantine_untracked` shells through sync's module-level
# `_git`, which would bypass the late-bound `core._git` seam every function here keeps
# for the ~25 `push._git` monkeypatch sites (see the module docstring).
from rebar._store.sync import _quarantine_dir_under, _untracked_overwrite_paths

# Bounded wait for the write lock around the push-retry merge (attempts=1, like sync.py's
# reconverge). A timeout means another writer holds the lock, so we skip the merge and
# leave the push pending rather than racing.
_PUSH_MERGE_LOCK_TIMEOUT = 15


# raw-git-ok: locked store seam internal
def _stash_create(core: ModuleType, base_path: str) -> str | None:
    """Record the dirty working tree as a stash COMMIT OBJECT, off the shared stack.

    ``git stash create`` writes the stash commit and prints its sha WITHOUT touching
    ``refs/stash``. That is the whole point (bug 2fa6): the stash stack is REPO-GLOBAL,
    shared by every worktree, so a ``stash push``/``pop`` pair here could — and did — pop an
    entry created on a source branch, dropping ``src/…`` into the store and stranding the
    index. A commit addressed by sha is unreachable from another worktree's pop. Unlike
    ``stash push``, ``create`` does NOT clean the working tree; the caller resets. Returns
    the sha, ``""`` when the tree was already clean, or ``None`` on git failure."""
    cp = core._git(base_path, "stash", "create", "push_tickets_branch:auto-stash")
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


# raw-git-ok: locked store seam internal
def _restore_stash(core: ModuleType, base_path: str, stash_sha: str) -> None:
    """Re-apply the stash commit built by :func:`_stash_create`, repairing a conflict.

    ``git stash apply <sha>`` names the commit explicitly, so it can only ever restore
    OUR OWN recorded tree — there is no stack to consult and nothing to ``drop``
    afterwards."""
    if not stash_sha:
        return  # nothing was stashed (clean tree)
    applied = core._git(base_path, "stash", "apply", "--quiet", stash_sha)
    _resolve_conflicted_apply(core, base_path, applied)


# raw-git-ok: locked store seam internal
def _resolve_conflicted_apply(
    core: ModuleType, base: str, applied: subprocess.CompletedProcess
) -> None:
    """Repair a ``git stash apply`` that applied-with-conflict (bug 6818).

    When the post-merge HEAD brings the upstream copy of a file in cleanly but the
    stashed edit touches the same region, the apply writes conflict markers and leaves an
    unmerged (UU) index entry — wedging the tracker (reconcile fail-closes on the markers;
    every ``git commit`` refuses the unmerged path). A clean apply never reaches here.

    The old code ASSUMED every conflicted path was reconciler-regenerable
    (``.bridge_state/prev_snapshot.json``, ``bindings.json``, ``get_rotation.json`` — rebuilt
    on the reconciler's next pass) and blind-restored it from HEAD. Bug 2fa6 disproved that,
    so the assumption is ENFORCED: each path is classified against what the branch tracks and
    the buckets diverge in :func:`~rebar._store.gitutil.discard_unmerged_paths` — a
    tracked path is restored from HEAD (the reconciler rebuilds it), while a path the
    branch does not track CANNOT be ticket data and is removed. Foreign paths are logged
    rather than vanishing silently: their presence means source files reached the tracker."""
    if applied.returncode == 0 and not core._git(base, "ls-files", "-u").stdout.strip():
        return  # genuinely clean apply — nothing to repair
    unmerged = sorted(set(core._git(base, "diff", "--name-only", "--diff-filter=U").stdout.split()))
    if not unmerged:
        return
    foreign = [p for p in unmerged if path_is_foreign_to_branch(base, p)]
    regenerable = [p for p in unmerged if p not in set(foreign)]
    if foreign:
        core.logger.warning(
            "tickets tracker held conflicted paths the branch does not track — removing: %s",
            ", ".join(foreign),
        )
    discard_unmerged_paths(base, regenerable, foreign)


def _recover_dirty_merge(
    core: ModuleType, base_path: str, remote_ref: str, attempt: int, strict: bool
) -> bool | None:
    """Set the dirty tree aside, merge, and restore the working-tree edits.

    Uses a stash COMMIT OBJECT (never ``refs/stash``) so nothing here can interact with
    the repo-global stash stack another worktree shares — see :func:`_stash_create`."""
    stash_sha = _stash_create(core, base_path)
    if stash_sha is None:
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            "stash failed during push recovery",
            base_path,
            remote_ref,
        )
        core.logger.warning("tickets branch push failed: stash failed (attempt %s)", attempt)
        return False
    if stash_sha:
        # ``stash create`` leaves the tree dirty; clear it so the merge can proceed.
        # Tracked files only — like ``stash push``, untracked files are left alone.
        # Restores the tree to HEAD AFTER ``stash create`` recorded it, so the recorded
        # state is never lost by this — the marker must sit on the call's own line.
        reset = core._git(  # raw-git-ok: locked store seam internal
            base_path, "reset", "--hard", "-q"
        )
        if reset.returncode != 0:
            _raise_if_strict(
                strict,
                "merge-recovery-blocked",
                "could not clear the working tree during push recovery",
                base_path,
                remote_ref,
            )
            core.logger.warning("tickets branch push failed: reset failed (attempt %s)", attempt)
            return False
    merge_target, problem = compat.store_epoch_merge_target(base_path, remote_ref)
    if merge_target is None or problem is not None:
        _restore_stash(core, base_path, stash_sha)
        _raise_if_strict(
            strict,
            "store-epoch-during-recovery",
            problem or "tickets store epoch guard could not pin remote ref",
            base_path,
            remote_ref,
        )
        core.logger.warning("%s", problem or "tickets store epoch guard could not pin remote ref")
        return None
    merge = core._git(
        base_path,
        "merge",
        merge_target,
        "--no-edit",
        "-m",
        f"Merge {remote_ref} (auto-reconcile, post-stash)",
    )
    if merge.returncode != 0:
        # Untracked-overwrite collisions are the one recoverable abort class here
        # (the reset above cleared TRACKED changes only): quarantine what git names,
        # retry ONCE. Any other failure falls through to today's abort net unchanged.
        merge = _retry_untracked_overwrite(core, base_path, merge_target, remote_ref, merge)
    if merge.returncode != 0:
        core._git(base_path, "merge", "--abort")
        _restore_stash(core, base_path, stash_sha)
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            merge.stderr or "merge failed after stash recovery",
            base_path,
            remote_ref,
        )
        core.logger.warning(
            "tickets branch merge failed after stash recovery (attempt %s)", attempt
        )
        return False
    _restore_stash(core, base_path, stash_sha)
    return True


def _retry_untracked_overwrite(
    core: ModuleType,
    base_path: str,
    merge_target: str,
    remote_ref: str,
    merge: subprocess.CompletedProcess,
) -> subprocess.CompletedProcess:
    """Self-heal the untracked-overwrite merge abort (parity with reconverge): when
    the merge failed ONLY because it wants to create paths that exist locally as
    untracked files (regenerable compaction leftovers), quarantine-move exactly what
    git names and retry the merge ONCE, returning the retry. Any other failure class
    — or a quarantine refusal — returns ``merge`` unchanged, so the caller keeps
    today's abort net exactly."""
    leftovers = _untracked_overwrite_paths(merge)
    if not leftovers:
        return merge
    core._git(base_path, "merge", "--abort")
    if not _quarantine_untracked_paths(core, base_path, leftovers):
        return merge
    return core._git(
        base_path,
        "merge",
        merge_target,
        "--no-edit",
        "-m",
        f"Merge {remote_ref} (auto-reconcile, post-stash)",
    )


def _quarantine_untracked_paths(core: ModuleType, base_path: str, paths: list[str]) -> bool:
    """Move (never delete) the named paths into the shared reconverge-quarantine dir.

    Local twin of sync's ``_quarantine_untracked``: the DIR PATH arithmetic is the
    shared ``sync._quarantine_dir_under``, but every git call runs through the
    late-bound ``core._git`` seam. The fence is checked for ALL paths before any
    move: a named path that is not genuinely UNTRACKED (``??``) refuses the whole
    recovery — a mis-parse must never relocate tracked data."""
    common = core._git(base_path, "rev-parse", "--git-common-dir").stdout.strip()
    if not common:
        return False
    for rel in paths:
        status = core._git(base_path, "status", "--porcelain", "-uall", "--", rel).stdout
        if not status.startswith("??"):
            return False
    quarantine = _quarantine_dir_under(common, base_path)
    tracker_root = Path(base_path)
    for rel in paths:
        src = tracker_root / rel
        if not src.exists():
            return False
        dest = quarantine / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return True


def _merge_with_transport_retry(
    core: ModuleType,
    base_path: str,
    remote_ref: str,
    merge_target: str,
    sleep_fn: Callable[[float], None] | None,
) -> subprocess.CompletedProcess:
    """Run the recovery merge, riding out a transport fault raised from INSIDE the merge.

    Bug f61c: the checkout is a ``blob:none`` partial clone, so ``git merge`` itself does
    on-demand promisor fetches. Run 31420498173 died as
    ``merge-recovery-blocked: ... server certificate verification failed`` together with
    ``could not fetch ... from promisor remote`` — a TRANSPORT fault wearing the merge
    reason. Retry only when the transport classifier matches; a genuine merge conflict is
    never transport-retriable and stays terminal on the first failure.
    """
    for transport_attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
        merge = core._git(
            base_path,
            "merge",
            merge_target,
            "--no-edit",
            "-m",
            f"Merge {remote_ref} (auto-reconcile during push retry)",
        )
        if merge.returncode == 0 or transport_attempt == _MAX_TRANSPORT_ATTEMPTS:
            return merge
        if not _is_transport_retriable(merge.stderr or ""):
            return merge
        core.logger.debug(
            "push-recovery merge hit a transient transport fault "
            "(transport attempt %s/%s); retrying automatically, no action needed: %s",
            transport_attempt,
            _MAX_TRANSPORT_ATTEMPTS,
            (merge.stderr or "").strip()[:200],
        )
        core._git(base_path, "merge", "--abort")
        _transport_backoff(transport_attempt, sleep_fn)
    return merge


def _merge_remote_under_lock(
    core: ModuleType,
    base_path: str,
    remote_ref: str,
    attempt: int,
    strict: bool,
    lock: Any,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool | None:
    """Merge the fetched remote ref while the store write lock is held."""
    try:
        lock.check_no_rebase_in_progress(base_path)
    except lock.RebaseGuard:
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            "tracker is in rebase or merge recovery state",
            base_path,
            remote_ref,
        )
        core.logger.warning(
            "cannot reconcile push — tracker is in rebase/merge recovery state. "
            "Run ticket-fsck-recover.sh."
        )
        return None
    merge_target, problem = compat.store_epoch_merge_target(base_path, remote_ref)
    if merge_target is None or problem is not None:
        _raise_if_strict(
            strict,
            "store-epoch-pre-merge",
            problem or "tickets store epoch guard could not pin remote ref",
            base_path,
            remote_ref,
        )
        core.logger.warning("%s", problem or "tickets store epoch guard could not pin remote ref")
        return None
    merge = _merge_with_transport_retry(core, base_path, remote_ref, merge_target, sleep_fn)
    if merge.returncode == 0:
        return True
    if _DIRTY_WD.search(merge.stderr or ""):
        return _recover_dirty_merge(core, base_path, remote_ref, attempt, strict)
    core._git(base_path, "merge", "--abort")
    _raise_if_strict(
        strict,
        "merge-recovery-blocked",
        merge.stderr or "merge conflict during push recovery",
        base_path,
        remote_ref,
    )
    core.logger.warning("tickets branch push failed (merge conflict, attempt %s)", attempt)
    return False


def _fetch_for_recovery(
    core: ModuleType,
    base_path: str,
    remote: str,
    branch: str,
    sleep_fn: Callable[[float], None] | None,
) -> subprocess.CompletedProcess:
    """Fetch the remote branch for merge recovery, riding out transient transport faults.

    Bug f61c: run 31420498173 lost recovery to `merge-recovery-blocked: ... server
    certificate verification failed`, so the fetch leg needs the same bounded transport
    retry as the push leg — a blob:none partial clone also fetches on demand here.
    """
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    for transport_attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
        fetch = core._git(base_path, "fetch", remote, refspec)
        if fetch.returncode == 0 or transport_attempt == _MAX_TRANSPORT_ATTEMPTS:
            return fetch
        if not _is_transport_retriable(fetch.stderr or ""):
            return fetch
        core.logger.debug(
            "push-recovery fetch hit a transient transport fault "
            "(transport attempt %s/%s); retrying automatically, no action needed: %s",
            transport_attempt,
            _MAX_TRANSPORT_ATTEMPTS,
            (fetch.stderr or "").strip()[:200],
        )
        _transport_backoff(transport_attempt, sleep_fn)
    return fetch


def _recover_non_fast_forward(
    core: ModuleType,
    base_path: str,
    remote: str,
    branch: str,
    remote_ref: str,
    attempt: int,
    strict: bool,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool | None:
    """Fetch and merge a genuine non-fast-forward rejection.

    ``True`` means a clean merge, ``False`` means a retryable local recovery
    failure, and ``None`` preserves the default path's terminal best-effort stop.
    """
    fetch = _fetch_for_recovery(core, base_path, remote, branch, sleep_fn)
    if fetch.returncode != 0:
        _raise_if_strict(
            strict,
            "push-transport-failed",
            fetch.stderr or "git fetch failed during push recovery",
            base_path,
            remote_ref,
        )
    from rebar._store import lock as _lock

    try:
        with _lock.write_lock(
            base_path, timeout=_PUSH_MERGE_LOCK_TIMEOUT, attempts=1, dual_window=True
        ):
            recovered = _merge_remote_under_lock(
                core, base_path, remote_ref, attempt, strict, _lock, sleep_fn
            )
        if recovered:
            # Bug ebee: back off before the caller re-pushes, so a lost CAS race gets a
            # window to converge instead of re-colliding with the same concurrent writer.
            _cas_backoff(attempt, sleep_fn)
        return recovered
    except _lock.LockTimeout:
        _raise_if_strict(
            strict,
            "lock-timeout",
            "write lock stayed busy during push recovery",
            base_path,
            remote_ref,
        )
        core.logger.warning(
            "tickets branch push-retry merge skipped: write lock busy; push stays pending"
        )
        return None
