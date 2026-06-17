"""Best-effort push of the tickets branch (Tier D, ``REBAR_WRITE_CORE``).

Faithful port of ``_push_tickets_branch`` (ticket-lib.sh). Honours the ``sync.push``
policy (``always`` | ``async`` | ``off``, default ``always``; env ``REBAR_SYNC_PUSH``,
deprecated alias ``REBAR_PUSH``, or a config file — resolved via the typed config),
pushes ``HEAD:tickets`` (the detached-HEAD commit, bug 27d8-b230), retries ≤3, and
reconciles a non-fast-forward by **merging** ``origin/tickets`` (never rebasing —
merge is atomic, no rebase-merge state to strand picks; 637b Fix 3), including the
dirty-working-tree stash→merge→pop dance (bug 12a6). ALWAYS returns ``None``
(best-effort): a push failure never fails the caller; ``fsck`` reports
``PUSH_PENDING`` while the local branch is ahead of origin.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

_NON_FF = re.compile(r"non-fast-forward|rejected|fetch first", re.IGNORECASE)
_DIRTY_WD = re.compile(
    r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE
)
_MAX_RETRIES = 3


def _push_mode(root: str | None = None) -> str:
    """The outbound push policy (``always`` | ``async`` | ``off``), resolved through
    the typed config (``sync.push``; env ``REBAR_SYNC_PUSH``, deprecated alias
    ``REBAR_PUSH``, or a config file). ``root`` is passed explicitly (the repo dir
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
        return subprocess.run(
            ["git", "-C", base, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", base, *args],
            124,
            "",
            f"git timed out after {_GIT_TIMEOUT}s",
        )


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
        except OSError:
            pass
        return

    # mode: always (default) — synchronous best-effort push.
    remote = _git(base_path, "remote").stdout.splitlines()
    if not remote or not remote[0].strip():
        return  # no remote — nothing to push

    push_env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
    for attempt in range(1, _MAX_RETRIES + 1):
        res = _git(base_path, "push", "origin", "HEAD:tickets", env=push_env)
        if res.returncode == 0:
            return
        stderr = res.stderr or ""
        if not _NON_FF.search(stderr):
            print(
                f"Warning: tickets branch push failed (exit {res.returncode}): {stderr}",
                file=sys.stderr,
            )
            return  # non-retriable class — best-effort

        # Non-fast-forward: reconcile by MERGE (not rebase).
        _git(base_path, "fetch", "origin", "tickets")
        from rebar._store import lock as _lock

        try:
            _lock.check_no_rebase_in_progress(base_path)
        except _lock.RebaseGuard:
            print(
                "Warning: cannot reconcile push — tracker is in rebase/merge recovery "
                "state. Run ticket-fsck-recover.sh.",
                file=sys.stderr,
            )
            return  # best-effort

        merge = _git(
            base_path,
            "merge",
            "origin/tickets",
            "--no-edit",
            "-m",
            "Merge origin/tickets (auto-reconcile during push retry)",
        )
        if merge.returncode == 0:
            continue  # merged clean — retry push next iter

        if _DIRTY_WD.search(merge.stderr or ""):
            # Dirty working tree (e.g. reconciler .bridge_state/* files): stash → merge → pop.
            stash = _git(
                base_path, "stash", "push", "--quiet", "-m", "_push_tickets_branch:auto-stash"
            )
            if stash.returncode != 0:
                print(
                    f"Warning: tickets branch push failed: stash failed (attempt {attempt})",
                    file=sys.stderr,
                )
                continue
            merge2 = _git(
                base_path,
                "merge",
                "origin/tickets",
                "--no-edit",
                "-m",
                "Merge origin/tickets (auto-reconcile, post-stash)",
            )
            _git(base_path, "stash", "pop", "--quiet")  # pop unconditionally
            if merge2.returncode != 0:
                _git(base_path, "merge", "--abort")
                print(
                    f"Warning: tickets branch merge failed after stash recovery "
                    f"(attempt {attempt})",
                    file=sys.stderr,
                )
            continue

        # Real content conflict — retry won't help, but continue so _MAX_RETRIES is honored.
        _git(base_path, "merge", "--abort")
        print(
            f"Warning: tickets branch push failed (merge conflict, attempt {attempt})",
            file=sys.stderr,
        )

    print(f"Warning: tickets branch push failed after {_MAX_RETRIES} retries", file=sys.stderr)


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

        push_tickets_branch(_lock.canonical_tracker(str(tracker)))
    except Exception:
        pass
