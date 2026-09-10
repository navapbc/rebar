"""Recover tickets-branch pushes after dirty trees or non-fast-forward rejection.

This call-graph cluster is entered through :func:`_recover_non_fast_forward`. Each function
accepts the calling ``push`` module and resolves ``core._git`` and ``core.logger`` at call time.
That preserves monkeypatch points and the established logger while avoiding an import cycle.
The dependency remains one-way from ``push`` to this module.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from types import ModuleType
from typing import Any

from rebar._store import compat, git_outcome, merge_recovery
from rebar._store.gitutil import discard_unmerged_paths, path_is_foreign_to_branch
from rebar._store.push_classify import (
    _DIRTY_WD,
    _MAX_TRANSPORT_ATTEMPTS,
    _cas_backoff,
    _is_transport_retriable,
    _raise_if_strict,
    _transport_backoff,
)

# :mod:`merge_recovery` owns the parser, quarantine paths, and mover shared with sync.
# Passing late-bound ``core._git`` preserves the push test seam.

# Bounded write-lock wait around the recovery merge. A timeout leaves the push pending instead
# of racing another writer.
_PUSH_MERGE_LOCK_TIMEOUT = 15


# raw-git-ok: locked store seam internal
def _stash_create(core: ModuleType, base_path: str) -> str | None:
    """Capture tracked dirty state in a stash commit outside the shared stack.

    ``git stash create`` does not touch ``refs/stash`` or clean the tree. This recovery
    only sets aside tracked modifications; untracked files remain in the
    working tree for quarantine if they collide. Return the SHA, ``""`` for a clean tree, or
    ``None`` on Git failure."""
    cp = core._git(base_path, "stash", "create", "push_tickets_branch:auto-stash")
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


# raw-git-ok: locked store seam internal
def _restore_stash(core: ModuleType, base_path: str, stash_sha: str) -> None:
    """Apply the exact commit from :func:`_stash_create` and repair conflicts.

    Addressing the SHA avoids the shared stash stack and requires no later drop."""
    if not stash_sha:
        return  # nothing was stashed (clean tree)
    applied = core._git(base_path, "stash", "apply", "--quiet", stash_sha)
    _resolve_conflicted_apply(core, base_path, applied)


# raw-git-ok: locked store seam internal
def _resolve_conflicted_apply(
    core: ModuleType, base: str, applied: subprocess.CompletedProcess
) -> None:
    """Repair an unmerged index left by ``git stash apply``.

    A clean apply returns unchanged. Conflicted branch-tracked files such as
    ``get_rotation.json`` are regenerable and restored from HEAD. Foreign paths cannot be
    ticket data, so they are logged and removed through
    :func:`~rebar._store.gitutil.discard_unmerged_paths`."""
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
    the repo-global stash stack another worktree shares. This is safe here because the
    ticket-store recovery only preserves tracked dirty files; it is not a held-out-oracle
    substitute for untracked tests — see :func:`_stash_create`."""
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
    leftovers = merge_recovery.untracked_overwrite_paths(merge)
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
    """Move (never delete) the named untracked paths into the shared reconverge-quarantine.

    A thin adapter over :func:`merge_recovery.quarantine_untracked` — the ONE mover —
    that resolves ``core._git`` at CALL time, so the late-bound seam survives the
    consolidation. The name is kept because ``_retry_untracked_overwrite`` resolves it at
    module scope and the push-recovery suite both calls and monkeypatches it."""
    return merge_recovery.quarantine_untracked(core._git, base_path, paths)


def _merge_with_transport_retry(
    core: ModuleType,
    base_path: str,
    remote_ref: str,
    merge_target: str,
    sleep_fn: Callable[[float], None] | None,
) -> subprocess.CompletedProcess:
    """Run the recovery merge with bounded transient-fault retries.

    Partial clones may fetch promisor objects during merge. Transport and transient filesystem
    failures therefore abort and retry with backoff. A merge conflict remains terminal on the
    first failure."""
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
        kind = git_outcome.classify(merge, operation=git_outcome.PUSH).kind
        if kind not in (git_outcome.GitKind.TRANSPORT, git_outcome.GitKind.TRANSIENT_FS):
            return merge
        core.logger.debug(
            "push-recovery merge hit a transient %s fault "
            "(attempt %s/%s); retrying automatically, no action needed: %s",
            kind.value,
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
    """Fetch the recovery branch with bounded retries for transport faults.

    This gives partial-clone fetches the same transient-failure policy as pushes."""
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
    """Back off, fetch, and merge a non-fast-forward rejection.

    ``True`` reports a clean merge. ``False`` reports a retryable local recovery failure.
    ``None`` preserves the terminal best-effort stop."""
    # Sleep before fetching so a concurrent CAS writer can land. Fetching and then merging
    # ensures the next push uses state observed after the wait.
    _cas_backoff(attempt, sleep_fn)
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
