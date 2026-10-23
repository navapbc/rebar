"""Push ``HEAD:tickets`` under the configured sync policy. Default delivery is best-effort;
strict delivery raises ``PushDeliveryError``, and S3 preflight can raise
``OptionalDependencyError`` regardless of strictness.

Non-fast-forward recovery merges the remote branch, preserving a dirty worktree with
a temporary stash commit outside the shared ``refs/stash`` stack. Failed delivery
leaves local commits for retry and appears as ``PUSH_PENDING`` in fsck.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

from rebar._optional import OptionalDependencyError
from rebar._store import push_recovery, push_state
from rebar._store.gitutil import credential_git_env, run_git, run_git_bounded
from rebar._store.push_classify import (
    _MAX_RETRIES,
    PushDeliveryError,
    _heal_multi_bundle_or_stop,
    _is_multi_bundle,
    _is_non_fast_forward,
    _raise_if_strict,
    _retry_transport_or_stop,
    _terminal_severity,
)

logger = logging.getLogger(__name__)


def _push_mode(root: str | None = None) -> str:
    """Resolve ``always``/``async``/``off`` from a bound gate or typed config.

    A bound gate's policy takes precedence so its signature writes do not push.
    Config discovery uses the explicit root without invoking git; malformed config
    falls back to ``always`` without breaking or disabling delivery.
    """
    from rebar._opcert_binding import current_push_mode

    bound = current_push_mode()
    if bound is not None:
        return bound
    from rebar import config

    return config.resolve_push_mode(root)


# Bound git calls (notably the network `push`) so a stuck remote can't hang the
# caller. Push is best-effort (a failure never fails the write), so a timeout
# surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30


# raw-git-ok: locked store seam internal
def _git(base: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """This module's bounded git seam — ~25 tests monkeypatch it, so the NAME stays; the
    timeout fold is the shared :func:`gitutil.run_git_bounded`."""
    return run_git_bounded(base, *args, env=env, timeout=_GIT_TIMEOUT, runner=run_git)


# Recovery lives in push_recovery.py. These shims pass this module at call time so
# patches to ``push._git`` still reach recovery; re-exports preserve public attributes
# and ``_resolve_conflicted_apply.__doc__``.
from rebar._store.push_recovery import (  # noqa: E402, F401 — re-exported for push.<symbol>
    _PUSH_MERGE_LOCK_TIMEOUT,
    _fetch_for_recovery,
    _merge_remote_under_lock,
    _merge_with_transport_retry,
    _resolve_conflicted_apply,
    _restore_stash,
    _stash_create,
)


def _recover_dirty_merge(
    base_path: str, remote_ref: str, attempt: int, strict: bool
) -> bool | None:
    return push_recovery._recover_dirty_merge(
        sys.modules[__name__], base_path, remote_ref, attempt, strict
    )


def _recover_non_fast_forward(
    base_path: str,
    remote: str,
    branch: str,
    remote_ref: str,
    attempt: int,
    strict: bool,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool | None:
    return push_recovery._recover_non_fast_forward(
        sys.modules[__name__], base_path, remote, branch, remote_ref, attempt, strict, sleep_fn
    )


def _require_s3_helper_if_s3_url(remote_url: str) -> None:
    """Require a compatible helper for S3 remotes regardless of ``strict``.

    Raise ``OptionalDependencyError`` for a missing or old helper; other schemes pass.
    """
    if remote_url.startswith("s3://") or remote_url.startswith("s3+zip://"):
        from rebar._optional import require_s3_helper

        require_s3_helper()


def _require_s3_helper_for_configured_remote(base_path: str) -> None:
    """Preflight S3 helpers in the async parent, whose child discards stderr.

    Skip this best-effort check when config or git is unavailable.
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


def push_tickets_branch(
    base_path: str,
    *,
    strict: bool = False,
    sleep_fn: Callable[[float], None] | None = None,
) -> None:
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
        # Spawn a detached synchronous push with a durable cwd. An unrelated child
        # needs git/ssh/proxy settings but no Jira credentials; project a fresh env.
        from rebar import _child_env
        from rebar._proc import spawn_detached

        child_env = _child_env.project_child_env(os.environ, relationship="unrelated")
        child_env["REBAR_SYNC_PUSH"] = "always"
        try:
            spawn_detached(
                "rebar._store.push",
                "push_tickets_branch",
                base_path,
                env=child_env,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            # Report a failed spawn; fsck still exposes the pending push.
            logger.warning("async tickets-branch push spawn failed: %r", exc)
        return

    # Resolve the synchronous destination from the main repo; malformed config
    # skips delivery rather than guessing a branch or remote.
    from rebar.config import ConfigError, tickets_branch, tickets_remote

    try:
        branch = tickets_branch(os.path.dirname(base_path))
        remote = tickets_remote(os.path.dirname(base_path))
    except ConfigError as exc:
        _raise_if_strict(strict, "invalid-destination", str(exc), base_path, remote_ref)
        return
    # A local-only store may lack its configured remote; fsck reports any backlog.
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

    push_env = credential_git_env({**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"})
    stderr = ""
    fifth_merge_clean = False
    healed_once = False
    # Bug f61c: transport attempts are counted SEPARATELY from the non-fast-forward budget
    # so a blip cannot silently consume the merge-recovery retries the non-FF path needs.
    transport_attempts = 1
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
            if not _heal_multi_bundle_or_stop(
                base_path, remote, branch, remote_ref, stderr, strict
            ):
                return
            healed_once = True
            continue
        if not _is_non_fast_forward(stderr):
            if _retry_transport_or_stop(
                base_path,
                remote_ref,
                stderr,
                res.returncode,
                strict,
                transport_attempts,
                sleep_fn,
            ):
                transport_attempts += 1
                continue
            return  # non-retriable class — best-effort

        recovered = _recover_non_fast_forward(
            base_path, remote, branch, remote_ref, attempt, strict, sleep_fn
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
        # Bug 3ff9: severity is computed BEFORE _raise_if_strict records the failure,
        # which overwrites the previous marker the backlog-growth check reads.
        level, backlog = _terminal_severity(base_path, remote_ref, terminal_detail)
        _raise_if_strict(
            strict,
            "final-push-rejected",
            terminal_detail,
            base_path,
            remote_ref,
        )
        logger.log(
            level,
            "tickets branch terminal push failed after recovery (exit %s): %s%s",
            terminal.returncode,
            terminal_detail,
            backlog,
        )
        return

    # Preserve the asserted "failed after N retries" wording. Severity follows
    # backlog growth: a non-growing contention race reports at INFO.
    level, backlog = _terminal_severity(base_path, remote_ref, stderr)
    _raise_if_strict(
        strict,
        "final-push-rejected",
        stderr or "push recovery exhausted",
        base_path,
        remote_ref,
    )
    logger.log(
        level,
        "tickets branch push failed after %s retries: %s%s",
        _MAX_RETRIES,
        stderr,
        backlog,
    )


def _ignore_lock_artifacts(base_path: str) -> bool:
    """Exclude lock files before staging, including in legacy stores.

    This runs before the write lock. One POSIX ``O_APPEND`` write prevents racing
    callers from tearing a pattern; duplicate complete lines are harmless.
    """
    resolved = _git(base_path, "rev-parse", "--git-path", "info/exclude")
    if resolved.returncode != 0 or not resolved.stdout.strip():
        return False
    exclude_path = resolved.stdout.strip()
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(base_path, exclude_path)
    try:
        with open(exclude_path, encoding="utf-8") as fh:
            existing = fh.read()
        patterns = (".ticket-write.lock", ".ticket-write.lock.d/")
        missing = [p for p in patterns if p not in existing.splitlines()]
        if missing:
            lead = "" if not existing or existing.endswith("\n") else "\n"
            payload = (lead + "\n".join(missing) + "\n").encode("utf-8")
            fd = os.open(exclude_path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
    except OSError:
        return False
    return True


def _porcelain_z_paths(porcelain_z: str) -> list[str]:
    """Worktree-relative paths named by ``git status --porcelain -z`` output.

    ``-z`` gives NUL-separated, unquoted records: ``XY <path>`` — and for a rename/copy
    the NEXT token is the origin path, which is consumed (the destination is what must
    be staged)."""
    tokens = porcelain_z.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token or len(token) < 4:
            continue
        paths.append(token[3:])
        if token[0] in "RC":
            i += 1  # skip the rename/copy origin token
    return paths


def _locked_commit_phase(
    canonical: str,
    *,
    message: str,
    paths: Sequence[str] | None,
    strict: bool,
    author_name: str | None,
    author_email: str | None,
) -> bool:
    """Stage and commit under the store lock, with optional scoped paths.

    ``paths=None`` stages all changes; a sequence stages only its paths, keeping
    unrelated bridge state out of selective snapshots. Return ``True`` for a commit
    or clean tree, ``False`` for a non-strict failure; strict failures raise.
    """
    from rebar._store import lock as _lock

    remote_ref = "origin/tickets"
    # A scoped `git status/add --` with ZERO paths would silently mean "everything",
    # inverting the caller's explicit selection — treat it as nothing to commit.
    if paths is not None and not tuple(paths):
        return True
    if not _ignore_lock_artifacts(canonical):
        detail = "could not exclude tracker lock artifacts from staging"
        _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
        logger.warning("tickets branch commit skipped: %s", detail)
        return False
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
                return False

            if paths is not None:
                # -z: NUL-separated + unquoted, so the dirty subset below is parseable
                # verbatim. A pathspec that matches nothing (a state file that does not
                # exist yet) is simply absent from the output rather than an error.
                dirty = _git(  # raw-git-ok: locked store seam internal (scoped status read)
                    canonical, "status", "--porcelain", "-z", "--", *paths
                )
            else:
                dirty = _git(canonical, "status", "--porcelain")
            if dirty.returncode != 0:
                detail = dirty.stderr or dirty.stdout or "git status failed"
                _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
                logger.warning("tickets branch commit skipped: %s", detail.strip())
                return False
            if dirty.stdout:
                if paths is not None:
                    # Stage only reported dirty paths. Anchor porcelain's root-relative
                    # names with ``:(top)`` so subdirectory trackers resolve them.
                    staged = _git(  # raw-git-ok: locked store seam internal
                        canonical,
                        "add",
                        "--",
                        *(f":(top){p}" for p in _porcelain_z_paths(dirty.stdout)),
                    )
                else:
                    staged = _git(canonical, "add", "-A")  # raw-git-ok: locked store seam internal
                if staged.returncode != 0:
                    detail = staged.stderr or staged.stdout or "git add failed"
                    _raise_if_strict(strict, "stage-failed", detail, canonical, remote_ref)
                    logger.warning("tickets branch commit skipped: %s", detail.strip())
                    return False

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
                    return False
    except _lock.LockTimeout as exc:
        _raise_if_strict(strict, "commit-lock-timeout", str(exc), canonical, remote_ref)
        logger.warning("tickets branch commit skipped: %s", exc)
        return False
    return True


def commit_tickets_branch(
    tracker: str | os.PathLike,
    *,
    message: str,
    paths: Sequence[str] | None = None,
    strict: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> bool:
    """Commit under the write lock without pushing; callers handle delivery.

    ``paths`` limits staging; ``None`` stages all changes. Return ``True`` for a
    commit or clean tree, ``False`` for non-strict failure; strict failure raises.
    """
    from rebar._store import lock as _lock

    return _locked_commit_phase(
        _lock.canonical_tracker(tracker),
        message=message,
        paths=paths,
        strict=strict,
        author_name=author_name,
        author_email=author_email,
    )


def commit_and_push_tickets_branch(
    tracker: str | os.PathLike,
    *,
    message: str,
    strict: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> None:
    """Commit under the lock, then push even if the tracker was clean.

    Non-strict failures retain pending changes; strict failures raise a classified
    ``PushDeliveryError``.
    """
    from rebar._store import lock as _lock

    canonical = _lock.canonical_tracker(tracker)
    if not _locked_commit_phase(
        canonical,
        message=message,
        paths=None,
        strict=strict,
        author_name=author_name,
        author_email=author_email,
    ):
        return

    push_tickets_branch(canonical, strict=strict)


def push_after_commit(tracker: str | os.PathLike) -> None:
    """Push commits from inline transition, reopen, claim, compact, and delete paths.

    Call after releasing the write lock: delivery may fetch and merge. The
    configured policy applies, and non-strict failures leave ``PUSH_PENDING``.
    """
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
