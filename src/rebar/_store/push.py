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
import subprocess
import sys
from collections.abc import Callable

from rebar._optional import OptionalDependencyError
from rebar._store import push_recovery, push_state
from rebar._store.gitutil import run_git
from rebar._store.push_classify import (
    _MAX_RETRIES,
    PushDeliveryError,
    _heal_multi_bundle_or_stop,
    _is_multi_bundle,
    _is_non_fast_forward,
    _raise_if_strict,
    _retry_transport_or_stop,
)
from rebar._store.push_state import unpushed_summary as _unpushed_summary

logger = logging.getLogger(__name__)


def _push_mode(root: str | None = None) -> str:
    """The outbound push policy (``always`` | ``async`` | ``off``), resolved through
    the typed config (``sync.push``; env ``REBAR_SYNC_PUSH`` or a config file).
    ``root`` is passed explicitly (the repo dir
    holding the tracker) so resolution is pure stat-based discovery — it never shells
    out to ``git`` for root detection, which would conflict with callers that mock
    subprocess. Best-effort: a malformed config falls back to the ``always`` default —
    a bad config must never break (or silently disable) the auto-push.

    STARTUP BINDING (story 6f14): when a bound op-cert gate is active it threads a
    context-local push policy (:func:`rebar._opcert_binding.current_push_mode`, ``off``
    for the gate service), which takes precedence over the env/config so the trusted gate
    never pushes a per-job SIGNATURE write — without patching ``REBAR_SYNC_PUSH`` in the
    process env."""
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
    try:
        return run_git(base, *args, check=False, env=env, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", base, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


# ── Push-recovery (stash/dirty-tree + non-fast-forward) now lives in push_recovery.py ──
# That cluster is monkeypatch-sensitive: ~25 tests patch ``push._git``, and 8 of the 9 moved
# functions shell out through it. The moved code resolves ``core._git`` / ``core.logger`` from
# the module handed in as ``core`` at CALL time, so these two shims pass THIS module and thereby
# preserve the historical ``push._recover_*`` call signatures — push_tickets_branch's call and
# test_push_shared_stash_2fa6's direct ``_recover_dirty_merge`` call both keep working against
# the patched ``push._git``. The remaining members are re-exported so ``push.<symbol>`` attribute
# access (and ``_resolve_conflicted_apply.__doc__``) survives the move.
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
        # Detach a synchronous push (REBAR_SYNC_PUSH=always) that survives parent exit.
        # The dispatcher launches the CLI as a bare `python3` whose `rebar`
        # importability comes from a parent sys.path bootstrap the child does NOT
        # inherit — so put the rebar `src` dir on the child's PYTHONPATH and have the
        # -c stub re-insert it (parents[2] of this file == .../src).
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # RP-04 S3 (AC4): a git-push child needs NO Jira send credential, so project the
        # parent env as an "unrelated" sibling — stripping every adapter-owned secret name
        # (JIRA_API_TOKEN / JIRA_PAT) while preserving all native git/ssh/proxy/CA config.
        # project_child_env returns a fresh dict and never mutates os.environ.
        from rebar import _child_env

        child_env = _child_env.project_child_env(os.environ, relationship="unrelated")
        child_env["REBAR_SYNC_PUSH"] = "always"
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
    """Keep the lock's own files out of Git even in an implicit-legacy store.

    Runs BEFORE the write lock (the exclusion must exist before anything stages), so two
    first-time callers race by construction. The append is therefore ONE ``os.write`` to an
    ``O_APPEND`` fd, applied atomically at end-of-file on POSIX (this project's declared
    support surface), so a race cannot tear a pattern into a half-line that would silently
    stop excluding it. The prior buffered two-write append merely happened to coalesce under
    CPython buffering; this makes that guarantee explicit rather than incidental. A racing
    pair can still append the same WHOLE line twice: tolerated by design, since git applies
    a repeated pattern identically and de-duplicating would need a lock forbidden here.
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
