"""Best-effort push of the tickets branch.

Faithful port of ``_push_tickets_branch`` (ticket-lib.sh). Honours the ``sync.push``
policy (``always`` | ``async`` | ``off``, default ``always``; env ``REBAR_SYNC_PUSH``
or a config file — resolved via the typed config),
pushes ``HEAD:tickets`` (the detached-HEAD commit, bug 27d8-b230), retries ≤3, and
reconciles a non-fast-forward by **merging** ``origin/tickets`` (never rebasing —
merge is atomic, no rebase-merge state to strand picks; 637b Fix 3), including the
dirty-working-tree stash→merge→pop dance (bug 12a6). ALWAYS returns ``None``
(best-effort): a push failure never fails the caller; ``fsck`` reports
``PUSH_PENDING`` while the local branch is ahead of origin.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Any

from rebar._store import compat
from rebar._store.gitutil import run_git

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


# raw-git-ok: locked store seam internal
def _unpushed_summary(base: str, remote_ref: str) -> str:
    """A ``" (N unpushed commits …)"`` suffix for a terminal push-failure warning.

    Bug 2a76: without it every failed write logged a byte-identical line, so a
    permanent outage looked like the same transient blip repeating. The count of
    ``<remote_ref>..HEAD`` makes the backlog ESCALATE across successive failed
    writes, which is the signal an operator (or fsck's PUSH_PENDING) acts on.
    Best-effort by construction: a push failure must never be turned into a crash,
    so an unresolvable count degrades to ``unknown`` (the remote-tracking ref can be
    absent on a store that has never fetched).
    """
    try:
        res = _git(base, "rev-list", "--count", f"{remote_ref}..HEAD")
        count = (res.stdout or "").strip()
        if res.returncode != 0 or not count.isdigit():
            count = "unknown"
    except Exception:  # noqa: BLE001 — diagnostics must never fail a best-effort push
        count = "unknown"
    return f" ({count} unpushed commits on {remote_ref}..HEAD)"


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
    """Raise a typed delivery failure while leaving default calls best-effort."""
    if strict:
        raise PushDeliveryError(reason, detail, base_path, remote_ref)


# raw-git-ok: locked store seam internal
def _resolve_conflicted_pop(base: str, stash: subprocess.CompletedProcess) -> None:
    """Repair a ``git stash pop`` that applied-with-conflict (bug 6818).

    The stashed working-tree edits live on reconciler-REGENERABLE files
    (``.bridge_state/prev_snapshot.json``, ``bindings.json``, ``get_rotation.json`` — rebuilt on the
    reconciler's next pass; a missing/empty prev_snapshot merely forces a full
    re-fetch). When the post-stash merge brings the upstream copy of such a file in
    cleanly but the stashed edit touches the same region, ``stash pop`` writes
    conflict markers into the working tree, leaves an unmerged (UU, stages 1/2/3)
    index entry, and KEEPS the stash — wedging the tracker (reconcile fail-closes
    on the markers; every ``git commit`` refuses the unmerged path).

    The clean-pop happy path never reaches here. On a conflicted pop we resolve
    deterministically: restore the conflicted path(s) to the merged HEAD (discarding
    the regenerable working-tree edit) and drop the now-applied stash so nothing
    dangles. After this the tree + index are consistent (no markers, no UU,
    committable)."""
    if stash.returncode == 0:
        # `stash pop` can still report rc 0 with no conflict — but be defensive and
        # check the index for unmerged entries left by an apply-with-conflict.
        if not _git(base, "ls-files", "-u").stdout.strip():
            return  # genuinely clean pop — nothing to repair
    unmerged = _git(base, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if unmerged:
        # Restore each conflicted path to the merged HEAD (committed) version: drops
        # the stashed regenerable edit AND the conflict markers. Remove the unmerged
        # index entries (all stages) THEN restore from HEAD, so no stranded stage is
        # left behind (a bare `checkout HEAD --` does not always clear the UU).
        _git(base, "rm", "-q", "--cached", "--", *unmerged)
        _git(base, "checkout", "HEAD", "--", *unmerged)
    # The stash was KEPT because the pop conflicted; the merged HEAD now carries the
    # upstream content we want, so drop the now-superseded stash to leave nothing
    # dangling. Best-effort (the top stash entry is the one we just popped).
    _git(base, "stash", "drop", "--quiet")


def _recover_dirty_merge(
    base_path: str, remote_ref: str, attempt: int, strict: bool
) -> bool | None:
    """Stash, merge, and restore regenerable working-tree edits."""
    stash = _git(
        base_path,
        "stash",
        "push",
        "--quiet",
        "-m",
        "push_tickets_branch:auto-stash",
    )
    if stash.returncode != 0:
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            "stash failed during push recovery",
            base_path,
            remote_ref,
        )
        logger.warning("tickets branch push failed: stash failed (attempt %s)", attempt)
        return False
    merge_target, problem = compat.store_epoch_merge_target(base_path, remote_ref)
    if merge_target is None or problem is not None:
        pop = _git(base_path, "stash", "pop", "--quiet")
        _resolve_conflicted_pop(base_path, pop)
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
        _git(base_path, "stash", "pop", "--quiet")
        _raise_if_strict(
            strict,
            "merge-recovery-blocked",
            merge.stderr or "merge failed after stash recovery",
            base_path,
            remote_ref,
        )
        logger.warning("tickets branch merge failed after stash recovery (attempt %s)", attempt)
        return False
    pop = _git(base_path, "stash", "pop", "--quiet")
    _resolve_conflicted_pop(base_path, pop)
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

    push_env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
    stderr = ""
    fifth_merge_clean = False
    for attempt in range(1, _MAX_RETRIES + 1):
        res = _git(base_path, "push", remote, f"HEAD:{branch}", env=push_env)
        if res.returncode == 0:
            return
        stderr = res.stderr or ""
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
        with _lock.write_lock(canonical, dual_window=True):
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
                staged = _git(canonical, "add", "-A")
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
                committed = _git(
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
