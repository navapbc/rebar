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

from rebar._store.gitutil import run_git

logger = logging.getLogger(__name__)

_NON_FF = re.compile(r"non-fast-forward|rejected|fetch first", re.IGNORECASE)
_DIRTY_WD = re.compile(
    r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE
)
_MAX_RETRIES = 3
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
    from rebar.config import ConfigError, load_config

    try:
        return load_config(root=root).sync.push
    except ConfigError:
        return "always"


# Bound git calls (notably the network `push`) so a stuck remote can't hang the
# caller. Push is best-effort (a failure never fails the write), so a timeout
# surfaces as a failed CompletedProcess, never a hang.
_GIT_TIMEOUT = 30


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


def _resolve_conflicted_pop(base: str, stash: subprocess.CompletedProcess) -> None:
    """Repair a ``git stash pop`` that applied-with-conflict (bug 6818).

    The stashed working-tree edits live on reconciler-REGENERABLE files
    (``.bridge_state/prev_snapshot.json``, ``bindings.json`` — rebuilt on the
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


def push_tickets_branch(base_path: str) -> None:
    """Push ``HEAD:tickets`` to origin per the ``sync.push`` policy (best-effort)."""
    mode = _push_mode(os.path.dirname(base_path))  # base_path is .../.tickets-tracker
    if mode == "off":
        return
    if mode == "async":
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
    except ConfigError:
        return
    # Guard on the CONFIGURED remote specifically (not "some remote exists"): if it is not a
    # configured git remote there is nothing to push to — skip quietly (a local-only store
    # is a supported mode, and fsck's PUSH_PENDING surfaces the unpushed commits).
    if _git(base_path, "remote", "get-url", remote).returncode != 0:
        return
    remote_ref = f"{remote}/{branch}"

    push_env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
    for attempt in range(1, _MAX_RETRIES + 1):
        res = _git(base_path, "push", remote, f"HEAD:{branch}", env=push_env)
        if res.returncode == 0:
            return
        stderr = res.stderr or ""
        if not _NON_FF.search(stderr):
            logger.warning("tickets branch push failed (exit %s): %s", res.returncode, stderr)
            return  # non-retriable class — best-effort

        # Non-fast-forward: reconcile by MERGE (not rebase). The fetch only moves
        # remote-tracking refs, so it stays OUTSIDE the write lock; the merge/stash
        # mutation below is taken UNDER the write lock (audit reliability #2). Without
        # the lock this fetch+merge ran concurrently with a foreground writer's commit,
        # spuriously failing that write against transient MERGE_HEAD/index state — the
        # exact hazard sync.py::reconverge already guards.
        _git(base_path, "fetch", remote, branch)
        from rebar._store import lock as _lock

        try:
            with _lock.write_lock(
                base_path, timeout=_PUSH_MERGE_LOCK_TIMEOUT, attempts=1, dual_window=True
            ):
                # Re-check INSIDE the lock (not only before acquiring it): a concurrent
                # reconverge/push could create MERGE_HEAD between a pre-lock check and lock
                # entry (TOCTOU). This mirrors sync.py::_do_reconverge's in-lock re-check.
                try:
                    _lock.check_no_rebase_in_progress(base_path)
                except _lock.RebaseGuard:
                    logger.warning(
                        "cannot reconcile push — tracker is in rebase/merge recovery "
                        "state. Run ticket-fsck-recover.sh."
                    )
                    return  # best-effort

                merge = _git(
                    base_path,
                    "merge",
                    remote_ref,
                    "--no-edit",
                    "-m",
                    f"Merge {remote_ref} (auto-reconcile during push retry)",
                )
                if merge.returncode == 0:
                    continue  # merged clean — retry push next iter

                if _DIRTY_WD.search(merge.stderr or ""):
                    # Dirty working tree (e.g. reconciler .bridge_state/* files): stash→merge→pop.
                    stash = _git(
                        base_path,
                        "stash",
                        "push",
                        "--quiet",
                        "-m",
                        "push_tickets_branch:auto-stash",
                    )
                    if stash.returncode != 0:
                        logger.warning(
                            "tickets branch push failed: stash failed (attempt %s)", attempt
                        )
                        continue
                    merge2 = _git(
                        base_path,
                        "merge",
                        remote_ref,
                        "--no-edit",
                        "-m",
                        f"Merge {remote_ref} (auto-reconcile, post-stash)",
                    )
                    if merge2.returncode != 0:
                        # Merge itself conflicted: the stash is still safely on the stack —
                        # abort the merge, then restore the working-tree edits so we don't
                        # strand them.
                        _git(base_path, "merge", "--abort")
                        _git(base_path, "stash", "pop", "--quiet")
                        logger.warning(
                            "tickets branch merge failed after stash recovery (attempt %s)", attempt
                        )
                        continue
                    # Merge succeeded; pop the stashed reconciler edits back. A clean pop is
                    # the happy path; an apply-with-conflict (markers + unmerged index, stash
                    # kept) is detected and repaired deterministically (bug 6818) so the tree
                    # is left consistent (no markers, no UU, committable).
                    pop = _git(base_path, "stash", "pop", "--quiet")
                    _resolve_conflicted_pop(base_path, pop)
                    continue

                # Real content conflict — retry won't help, but continue so _MAX_RETRIES is honored.
                _git(base_path, "merge", "--abort")
                logger.warning("tickets branch push failed (merge conflict, attempt %s)", attempt)
        except _lock.LockTimeout:
            # Could not get the write lock in the bounded window — another writer/syncer
            # holds it. Skip the merge and leave the push PENDING (best-effort) rather than
            # racing a concurrent write; fsck surfaces PUSH_PENDING. Never fail the write.
            logger.warning(
                "tickets branch push-retry merge skipped: write lock busy; push stays pending"
            )
            return

    logger.warning("tickets branch push failed after %s retries", _MAX_RETRIES)


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
    except Exception:  # noqa: BLE001 — best-effort async push; broad-but-logged, fsck surfaces PUSH_PENDING
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
