"""Best-effort push of the tickets branch.

Faithful port of ``_push_tickets_branch`` (ticket-lib.sh). Honours the ``sync.push``
policy (``always`` | ``async`` | ``off``, default ``always``; env ``REBAR_SYNC_PUSH``
or a config file — resolved via the typed config),
pushes ``HEAD:tickets`` (the detached-HEAD commit, bug 27d8-b230), retries ≤3, and
reconciles a non-fast-forward by **merging** ``origin/tickets`` (never rebasing —
merge is atomic, no rebase-merge state to strand picks; 637b Fix 3), including the
dirty-working-tree set-aside→merge→restore dance (bug 12a6) — which uses a stash COMMIT
OBJECT, never the repo-global ``refs/stash`` stack every worktree shares (bug 2fa6).
ALWAYS returns ``None`` (best-effort): a push failure never fails the caller; ``fsck``
reports ``PUSH_PENDING`` while the local branch is ahead of origin.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Any

from rebar._optional import OptionalDependencyError
from rebar._store import compat, push_state
from rebar._store.gitutil import discard_unmerged_paths, path_is_foreign_to_branch, run_git
from rebar._store.push_state import unpushed_summary as _unpushed_summary

logger = logging.getLogger(__name__)

_NON_FF = re.compile(r"non-fast-forward|rejected|fetch first", re.IGNORECASE)

# Bug 2a76: the bare token ``rejected`` above is NOT specific to a non-fast-forward.
# git prints ``! [remote rejected] HEAD -> tickets (pre-receive hook declined)`` for
# EVERY server-side decline — GitHub push protection (GH013 secret scanning), a
# pre-receive hook, branch protection, a rate limit, an internal server error. Those
# are PERMANENT policy rejections: a fetch+merge cannot fix them, so classifying them
# as non-fast-forward burned all three retries (three real hits on the remote's hook,
# zero merge commits) and then reported only "failed after 3 retries" — the reason git
# gave us was thrown away, making an 8-hour outage indistinguishable from transient
# contention while commits piled up locally. The fix is the same SUBTRACTIVE exclusion
# shape already proven in _engine/rebar_reconciler/_ref_lock.py (bug 4afc): a broad
# marker counts only when nothing names a non-mergeable cause.
_POLICY_DECLINE_MARKERS = (
    "hook declined",  # pre-receive / update hook (incl. GitHub push protection GH013)
    "push declined",
    "protected branch",
    "branch protection",
    "internal server error",
    "rate limit",
    "gh0",  # GitHub push-protection / policy error codes: GH006, GH013, ...
)


def _is_policy_decline(stderr: str) -> bool:
    """Whether the remote explicitly declined the push for a policy reason."""
    return any(marker in stderr.lower() for marker in _POLICY_DECLINE_MARKERS)


def _is_non_fast_forward(stderr: str) -> bool:
    """Whether *stderr* shows a genuine non-fast-forward (retriable by fetch+merge).

    A policy decline also carries the word ``rejected``, so it must be excluded
    explicitly; ambiguity resolves to TERMINAL (report the reason once) rather than
    to a retry loop that provably cannot converge (bug 2a76).
    """
    if _is_policy_decline(stderr):
        return False
    return bool(_NON_FF.search(stderr))


def _is_multi_bundle(stderr: str) -> bool:
    """Whether *stderr* shows the git-remote-s3 multi-bundle state (a ref with two bundles).

    True when the message reports ``multiple bundles`` or ``multiple updates for ref``
    (case-insensitive); False for a plain non-fast-forward or a transport error.
    """
    low = stderr.lower()
    return "multiple bundles" in low or "multiple updates for ref" in low


_DIRTY_WD = re.compile(
    r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE
)
_MAX_RETRIES = 5
# Bounded wait for the write lock around the push-retry merge (attempts=1, like sync.py's
# reconverge). A timeout means another writer holds the lock, so we skip the merge and
# leave the push pending rather than racing.
_PUSH_MERGE_LOCK_TIMEOUT = 15


def _push_mode(root: str | None = None) -> str:
    """The outbound push policy (``always`` | ``async`` | ``off``), resolved through
    the typed config (``sync.push``; env ``REBAR_SYNC_PUSH`` or a config file).
    ``root`` is passed explicitly (the repo dir
    holding the tracker) so resolution is pure stat-based discovery — it never shells
    out to ``git`` for root detection, which would conflict with callers that mock
    subprocess. Best-effort: a malformed config falls back to the ``always`` default —
    a bad config must never break (or silently disable) the auto-push."""
    from rebar._store._push_policy import normalize_push_mode
    from rebar.config import ConfigError, load_config

    try:
        return normalize_push_mode(load_config(root=root).sync.push)
    except ConfigError:
        return "always"


# Bound git calls (notably the network `push`) so a stuck remote can't hang the
# caller. Push is best-effort (a failure never fails the write), so a timeout
# surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30


# raw-git-ok: locked store seam internal
def _git(base: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return run_git(base, *args, check=False, env=env, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", base, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


class PushDeliveryError(RuntimeError):
    """A strict tickets-branch delivery failure with a stable classification."""

    def __init__(self, reason: str, detail: str, base_path: str, remote_ref: str) -> None:
        self.reason = reason
        self.detail = detail
        self.message = f"{reason}: {detail}{_unpushed_summary(base_path, remote_ref)}"
        super().__init__(self.message)


def _raise_if_strict(
    strict: bool, reason: str, detail: str, base_path: str, remote_ref: str
) -> None:
    """Record the delivery outcome, then raise only for a strict caller.

    Every terminal exit in this module already routes through here carrying the closed set
    of :class:`PushDeliveryError` reasons, which makes it the one place a failure cannot be
    missed — so the durable marker is written HERE rather than at a dozen call sites. The
    default (best-effort) path still returns ``None``: recording is a SIGNAL, not a raise.
    """
    push_state.record_failure(base_path, reason, detail, remote_ref)
    if strict:
        raise PushDeliveryError(reason, detail, base_path, remote_ref)


# raw-git-ok: locked store seam internal
def _stash_create(base_path: str) -> str | None:
    """Record the dirty working tree as a stash COMMIT OBJECT, off the shared stack.

    ``git stash create`` writes the stash commit and prints its sha WITHOUT touching
    ``refs/stash``. That is the whole point (bug 2fa6): the stash stack is REPO-GLOBAL,
    shared by every worktree, so a ``stash push``/``pop`` pair here could — and did — pop an
    entry created on a source branch, dropping ``src/…`` into the store and stranding the
    index. A commit addressed by sha is unreachable from another worktree's pop. Unlike
    ``stash push``, ``create`` does NOT clean the working tree; the caller resets. Returns
    the sha, ``""`` when the tree was already clean, or ``None`` on git failure."""
    cp = _git(base_path, "stash", "create", "push_tickets_branch:auto-stash")
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


# raw-git-ok: locked store seam internal
def _restore_stash(base_path: str, stash_sha: str) -> None:
    """Re-apply the stash commit built by :func:`_stash_create`, repairing a conflict.

    ``git stash apply <sha>`` names the commit explicitly, so it can only ever restore
    OUR OWN recorded tree — there is no stack to consult and nothing to ``drop``
    afterwards."""
    if not stash_sha:
        return  # nothing was stashed (clean tree)
    applied = _git(base_path, "stash", "apply", "--quiet", stash_sha)
    _resolve_conflicted_apply(base_path, applied)


# raw-git-ok: locked store seam internal
def _resolve_conflicted_apply(base: str, applied: subprocess.CompletedProcess) -> None:
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
    if applied.returncode == 0 and not _git(base, "ls-files", "-u").stdout.strip():
        return  # genuinely clean apply — nothing to repair
    unmerged = sorted(set(_git(base, "diff", "--name-only", "--diff-filter=U").stdout.split()))
    if not unmerged:
        return
    foreign = [p for p in unmerged if path_is_foreign_to_branch(base, p)]
    regenerable = [p for p in unmerged if p not in set(foreign)]
    if foreign:
        logger.warning(
            "tickets tracker held conflicted paths the branch does not track — removing: %s",
            ", ".join(foreign),
        )
    discard_unmerged_paths(base, regenerable, foreign)


def _recover_dirty_merge(
    base_path: str, remote_ref: str, attempt: int, strict: bool
) -> bool | None:
    """Set the dirty tree aside, merge, and restore the working-tree edits.

    Uses a stash COMMIT OBJECT (never ``refs/stash``) so nothing here can interact with
    the repo-global stash stack another worktree shares — see :func:`_stash_create`."""
    stash_sha = _stash_create(base_path)
    if stash_sha is None:
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            "stash failed during push recovery",
            base_path,
            remote_ref,
        )
        logger.warning("tickets branch push failed: stash failed (attempt %s)", attempt)
        return False
    if stash_sha:
        # ``stash create`` leaves the tree dirty; clear it so the merge can proceed.
        # Tracked files only — like ``stash push``, untracked files are left alone.
        # Restores the tree to HEAD AFTER ``stash create`` recorded it, so the recorded
        # state is never lost by this — the marker must sit on the call's own line.
        reset = _git(  # raw-git-ok: locked store seam internal
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
            logger.warning("tickets branch push failed: reset failed (attempt %s)", attempt)
            return False
    merge_target, problem = compat.store_epoch_merge_target(base_path, remote_ref)
    if merge_target is None or problem is not None:
        _restore_stash(base_path, stash_sha)
        _raise_if_strict(
            strict,
            "store-epoch-during-recovery",
            problem or "tickets store epoch guard could not pin remote ref",
            base_path,
            remote_ref,
        )
        logger.warning("%s", problem or "tickets store epoch guard could not pin remote ref")
        return None
    merge = _git(
        base_path,
        "merge",
        merge_target,
        "--no-edit",
        "-m",
        f"Merge {remote_ref} (auto-reconcile, post-stash)",
    )
    if merge.returncode != 0:
        _git(base_path, "merge", "--abort")
        _restore_stash(base_path, stash_sha)
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            merge.stderr or "merge failed after stash recovery",
            base_path,
            remote_ref,
        )
        logger.warning("tickets branch merge failed after stash recovery (attempt %s)", attempt)
        return False
    _restore_stash(base_path, stash_sha)
    return True


def _merge_remote_under_lock(
    base_path: str, remote_ref: str, attempt: int, strict: bool, lock: Any
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
        logger.warning(
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
        logger.warning("%s", problem or "tickets store epoch guard could not pin remote ref")
        return None
    merge = _git(
        base_path,
        "merge",
        merge_target,
        "--no-edit",
        "-m",
        f"Merge {remote_ref} (auto-reconcile during push retry)",
    )
    if merge.returncode == 0:
        return True
    if _DIRTY_WD.search(merge.stderr or ""):
        return _recover_dirty_merge(base_path, remote_ref, attempt, strict)
    _git(base_path, "merge", "--abort")
    _raise_if_strict(
        strict,
        "merge-recovery-blocked",
        merge.stderr or "merge conflict during push recovery",
        base_path,
        remote_ref,
    )
    logger.warning("tickets branch push failed (merge conflict, attempt %s)", attempt)
    return False


def _recover_non_fast_forward(
    base_path: str, remote: str, branch: str, remote_ref: str, attempt: int, strict: bool
) -> bool | None:
    """Fetch and merge a genuine non-fast-forward rejection.

    ``True`` means a clean merge, ``False`` means a retryable local recovery
    failure, and ``None`` preserves the default path's terminal best-effort stop.
    """
    fetch = _git(base_path, "fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
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
            return _merge_remote_under_lock(base_path, remote_ref, attempt, strict, _lock)
    except _lock.LockTimeout:
        _raise_if_strict(
            strict,
            "lock-timeout",
            "write lock stayed busy during push recovery",
            base_path,
            remote_ref,
        )
        logger.warning(
            "tickets branch push-retry merge skipped: write lock busy; push stays pending"
        )
        return None


def _require_s3_helper_if_s3_url(remote_url: str) -> None:
    """Fail closed on an s3://|s3+zip:// remote whose git-remote-s3 helper is missing/too old.

    A deliberate exception to push_tickets_branch's best-effort contract: raises
    OptionalDependencyError UNCONDITIONALLY (independent of `strict`), because a misconfigured
    S3 remote must halt loudly rather than be swallowed. A no-op for any other scheme.
    """
    if remote_url.startswith("s3://") or remote_url.startswith("s3+zip://"):
        from rebar._optional import require_s3_helper

        require_s3_helper()


def _require_s3_helper_for_configured_remote(base_path: str) -> None:
    """Resolve the configured remote for ``base_path`` and fail closed if it is an S3 URL.

    Used by the async delivery arm to surface a misconfigured-S3 error IN THE PARENT (the
    detached child's stderr is discarded). A malformed config or an unavailable git binary
    simply skips this best-effort preflight.
    """
    from rebar.config import ConfigError, tickets_remote

    try:
        remote = tickets_remote(os.path.dirname(base_path))
    except ConfigError:
        return
    try:
        resolved = _git(base_path, "remote", "get-url", remote)
    except OSError:
        return
    if resolved.returncode == 0:
        _require_s3_helper_if_s3_url(resolved.stdout.strip())


def push_tickets_branch(base_path: str, *, strict: bool = False) -> None:
    """Push ``HEAD:tickets`` according to the configured delivery policy.

    The default remains best-effort: warnings leave the local commit and working
    tree intact.  Strict callers receive :class:`PushDeliveryError` instead.
    """
    remote_ref = "origin/tickets"
    mode = _push_mode(os.path.dirname(base_path))  # base_path is .../.tickets-tracker
    if mode == "off":
        _raise_if_strict(
            strict,
            "push-disabled",
            "sync.push is off, so delivery is disabled",
            base_path,
            remote_ref,
        )
        return
    if mode == "async":
        _raise_if_strict(
            strict,
            "async-delivery-unobservable",
            "sync.push is async, so synchronous delivery cannot be observed",
            base_path,
            remote_ref,
        )
        # Fail fast IN THE PARENT on a misconfigured S3 remote — the detached child's stderr
        # is discarded, so the actionable message would otherwise be lost.
        _require_s3_helper_for_configured_remote(base_path)
        # Detach a synchronous push (REBAR_SYNC_PUSH=always) that survives parent exit.
        # The dispatcher launches the CLI as a bare `python3` whose `rebar`
        # importability comes from a parent sys.path bootstrap the child does NOT
        # inherit — so put the rebar `src` dir on the child's PYTHONPATH and have the
        # -c stub re-insert it (parents[2] of this file == .../src).
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        child_env = {**os.environ, "REBAR_SYNC_PUSH": "always"}
        child_env["PYTHONPATH"] = src + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[2]); "
                    "from rebar._store import push; push.push_tickets_branch(sys.argv[1])",
                    base_path,
                    src,
                ],
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # orphan it (own session); survives parent exit
                close_fds=True,
            )
        except OSError as exc:
            # Observability gap (audit 3.2): a failed detached spawn used to be swallowed
            # silently, so an async push that never started looked identical to one that
            # succeeded. Log it; the push simply stays pending (fsck surfaces PUSH_PENDING).
            logger.warning("async tickets-branch push spawn failed: %r", exc)
        return

    # mode: always (default) — synchronous best-effort push.
    # Branch + remote resolved from the MAIN repo config (the tracker's parent), matching
    # _push_mode. Best-effort: on a malformed config, skip rather than push to a guessed
    # branch/remote (a wrong refspec would publish to the wrong place).
    from rebar.config import ConfigError, tickets_branch, tickets_remote

    try:
        branch = tickets_branch(os.path.dirname(base_path))
        remote = tickets_remote(os.path.dirname(base_path))
    except ConfigError as exc:
        _raise_if_strict(strict, "invalid-destination", str(exc), base_path, remote_ref)
        return
    # Guard on the CONFIGURED remote specifically (not "some remote exists"): if it is not a
    # configured git remote there is nothing to push to — skip quietly (a local-only store
    # is a supported mode, and fsck's PUSH_PENDING surfaces the unpushed commits).
    remote_url = _git(base_path, "remote", "get-url", remote)
    if remote_url.returncode != 0:
        _raise_if_strict(
            strict,
            "remote-not-found",
            remote_url.stderr or f"configured remote {remote!r} is unavailable",
            base_path,
            f"{remote}/{branch}",
        )
        return
    remote_ref = f"{remote}/{branch}"
    _require_s3_helper_if_s3_url(remote_url.stdout.strip())

    push_env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
    stderr = ""
    fifth_merge_clean = False
    healed_once = False
    for attempt in range(1, _MAX_RETRIES + 1):
        res = _git(base_path, "push", remote, f"HEAD:{branch}", env=push_env)
        if res.returncode == 0:
            # The backlog is delivered: drop any marker an earlier failure left, so the
            # pending signal cannot latch on past the outage it described.
            push_state.clear(base_path)
            return
        stderr = res.stderr or ""
        # Heal git-remote-s3's multi-bundle state BEFORE the non-FF classification: the merge
        # collapses the divergent bundles losslessly, then we retry the push once.
        if _is_multi_bundle(stderr) and not healed_once:
            from rebar._store import s3_doctor

            try:
                s3_doctor.heal_multi_bundle(base_path, remote, branch)
            except s3_doctor.S3DoctorConflict as exc:
                logger.warning(
                    "s3 doctor could not heal multi-bundle ref %s: %s (%s)%s",
                    remote_ref,
                    exc,
                    exc.hint,
                    _unpushed_summary(base_path, remote_ref),
                )
                _raise_if_strict(
                    strict, "push-multi-bundle-conflict", str(exc), base_path, remote_ref
                )
                return
            except OptionalDependencyError as exc:
                logger.warning("s3 doctor unavailable for multi-bundle heal: %s", exc)
                _raise_if_strict(strict, "push-transport-failed", stderr, base_path, remote_ref)
                return
            healed_once = True
            continue
        if not _is_non_fast_forward(stderr):
            # Terminal: a transport failure OR a policy decline (bug 2a76). Report the
            # reason git gave AND the backlog size, then stop — hitting the remote twice
            # more cannot change a permanent rule violation.
            reason = (
                "push-policy-declined" if _is_policy_decline(stderr) else "push-transport-failed"
            )
            _raise_if_strict(strict, reason, stderr, base_path, remote_ref)
            logger.warning(
                "tickets branch push failed (exit %s): %s%s",
                res.returncode,
                stderr,
                _unpushed_summary(base_path, remote_ref),
            )
            return  # non-retriable class — best-effort

        recovered = _recover_non_fast_forward(
            base_path, remote, branch, remote_ref, attempt, strict
        )
        if recovered is None:
            return
        fifth_merge_clean = recovered and attempt == _MAX_RETRIES

    if fifth_merge_clean:
        terminal = _git(base_path, "push", remote, f"HEAD:{branch}", env=push_env)
        if terminal.returncode == 0:
            push_state.clear(base_path)
            return
        terminal_detail = terminal.stderr or "terminal push after recovery was rejected"
        _raise_if_strict(
            strict,
            "final-push-rejected",
            terminal_detail,
            base_path,
            remote_ref,
        )
        logger.warning(
            "tickets branch terminal push failed after recovery (exit %s): %s%s",
            terminal.returncode,
            terminal_detail,
            _unpushed_summary(base_path, remote_ref),
        )
        return

    # Keep the literal "failed after N retries" wording — two negative assertions
    # (test_epoch_guard_matrix / test_epoch_guard_reconciliation) prove this line is
    # ABSENT in their scenarios, so it is appended to, never reworded. Bug 2a76 adds
    # the last rejection reason and the escalating backlog size.
    _raise_if_strict(
        strict,
        "final-push-rejected",
        stderr or "push recovery exhausted",
        base_path,
        remote_ref,
    )
    logger.warning(
        "tickets branch push failed after %s retries: %s%s",
        _MAX_RETRIES,
        stderr,
        _unpushed_summary(base_path, remote_ref),
    )


def _ignore_lock_artifacts(base_path: str) -> bool:
    """Keep the lock's own files out of Git even in an implicit-legacy store."""
    resolved = _git(base_path, "rev-parse", "--git-path", "info/exclude")
    if resolved.returncode != 0 or not resolved.stdout.strip():
        return False
    exclude_path = resolved.stdout.strip()
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(base_path, exclude_path)
    try:
        with open(exclude_path, encoding="utf-8") as fh:
            existing = fh.read()
        missing = [
            pattern
            for pattern in (".ticket-write.lock", ".ticket-write.lock.d/")
            if pattern not in existing.splitlines()
        ]
        if missing:
            with open(exclude_path, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write("\n".join(missing) + "\n")
    except OSError:
        return False
    return True


def commit_and_push_tickets_branch(
    tracker: str | os.PathLike,
    *,
    message: str,
    strict: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> None:
    """Commit all pending tracker changes under its write lock, then push.

    A clean tracker can still be ahead of its remote, so delivery always follows a
    successful locked phase.  Default callers retain pending content after a local
    failure and return best-effort; strict callers receive a classified
    :class:`PushDeliveryError`.
    """
    from rebar._store import lock as _lock

    canonical = _lock.canonical_tracker(tracker)
    remote_ref = "origin/tickets"
    if not _ignore_lock_artifacts(canonical):
        detail = "could not exclude tracker lock artifacts from staging"
        _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
        logger.warning("tickets branch commit skipped: %s", detail)
        return
    try:
        with _lock.write_lock(canonical, dual_window=True, retries=_lock.write_path_retries()):
            try:
                _lock.check_no_rebase_in_progress(canonical)
            except _lock.RebaseGuard:
                _raise_if_strict(
                    strict,
                    "merge-recovery-blocked",
                    "tracker is in rebase or merge recovery state",
                    canonical,
                    remote_ref,
                )
                logger.warning(
                    "tickets branch commit skipped: tracker is in rebase/merge recovery state"
                )
                return

            dirty = _git(canonical, "status", "--porcelain")
            if dirty.returncode != 0:
                detail = dirty.stderr or dirty.stdout or "git status failed"
                _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
                logger.warning("tickets branch commit skipped: %s", detail.strip())
                return
            if dirty.stdout:
                staged = _git(canonical, "add", "-A")  # raw-git-ok: locked store seam internal
                if staged.returncode != 0:
                    detail = staged.stderr or staged.stdout or "git add failed"
                    _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
                    logger.warning("tickets branch commit skipped: %s", detail.strip())
                    return

                identity: list[str] = []
                if author_name is not None:
                    identity.extend(("-c", f"user.name={author_name}"))
                if author_email is not None:
                    identity.extend(("-c", f"user.email={author_email}"))
                committed = _git(  # raw-git-ok: locked store seam internal
                    canonical,
                    *identity,
                    "commit",
                    "-q",
                    "--no-verify",
                    "-m",
                    message,
                )
                if committed.returncode != 0:
                    detail = committed.stderr or committed.stdout or "git commit failed"
                    _raise_if_strict(strict, "commit-failed", detail, canonical, remote_ref)
                    logger.warning("tickets branch commit skipped: %s", detail.strip())
                    return
    except _lock.LockTimeout as exc:
        _raise_if_strict(strict, "commit-lock-timeout", str(exc), canonical, remote_ref)
        logger.warning("tickets branch commit skipped: %s", exc)
        return

    push_tickets_branch(canonical, strict=strict)


def push_after_commit(tracker: str | os.PathLike) -> None:
    """Best-effort auto-push for the inline-commit write paths.

    ``transition`` / ``reopen`` / ``claim`` (txn.py), ``compact`` (compact.py), and
    ``delete`` (delete.py) do their own locked rename+commit rather than going
    through :func:`rebar._store.event_append.write_and_push`, so they must trigger
    the same best-effort push the ``append_event`` family gets — otherwise a
    trailing status/compact/delete (the LAST write of a session) strands its commit
    as ``PUSH_PENDING`` (bug ``prone-octet-cheek``). Resolves the canonical tracker
    and pushes ``HEAD:tickets`` per the ``sync.push`` policy; never raises
    (``push_tickets_branch`` is itself best-effort). Call AFTER the locked commit
    has released the store lock — the push runs its own fetch/merge and must not
    nest inside the write lock."""
    try:
        from rebar._store import lock as _lock

        canonical = _lock.canonical_tracker(str(tracker))
        push_tickets_branch(canonical)
    except OptionalDependencyError:
        # A misconfigured S3 remote is a deliberate fail-closed override of the best-effort
        # contract (mirrors write_and_push's uncaught call): let it escape so the inline-commit
        # write paths halt with the actionable install message instead of silently swallowing it.
        raise
    except Exception:
        logger.warning(
            "best-effort tickets-branch push failed; PUSH_PENDING will surface via fsck",
            exc_info=True,
        )
        return
    # Opportunistic enrichment drain on the status-only write paths too (epic only-crave-art
    # / c1de), so the drain rides BOTH push paths. Best-effort — never fails the write.
    try:
        from rebar.llm.enrich_drain import maybe_drain

        maybe_drain(str(canonical))
    except Exception:  # noqa: BLE001 — a drain concern must never fail a write
        pass


def _main(argv: list[str] | None = None) -> int:
    """Private process boundary for strict tickets-branch delivery."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m rebar._store.push")
    commands = parser.add_subparsers(dest="command", required=True)
    push_parser = commands.add_parser("push")
    push_parser.add_argument("--tracker", required=True)
    push_parser.add_argument("--strict", action="store_true")
    commit_parser = commands.add_parser("commit-and-push")
    commit_parser.add_argument("--tracker", required=True)
    commit_parser.add_argument("--message", required=True)
    commit_parser.add_argument("--strict", action="store_true")
    commit_parser.add_argument("--author-name")
    commit_parser.add_argument("--author-email")
    parsed = parser.parse_args(argv)
    try:
        if parsed.command == "push":
            push_tickets_branch(parsed.tracker, strict=parsed.strict)
        elif parsed.command == "commit-and-push":
            commit_and_push_tickets_branch(
                parsed.tracker,
                message=parsed.message,
                strict=parsed.strict,
                author_name=parsed.author_name,
                author_email=parsed.author_email,
            )
        else:  # pragma: no cover - argparse constrains this today.
            raise AssertionError(f"unhandled push command: {parsed.command!r}")
    except PushDeliveryError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
