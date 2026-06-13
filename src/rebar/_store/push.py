"""Best-effort push of the tickets branch (Tier D, ``REBAR_WRITE_CORE``).

Faithful port of ``_push_tickets_branch`` (ticket-lib.sh). Honours ``REBAR_PUSH``
(``always`` | ``async`` | ``off``, default ``always``; case/space-insensitive),
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
_DIRTY_WD = re.compile(r"would be overwritten by merge|local changes.*would be overwritten", re.IGNORECASE)
_MAX_RETRIES = 3


def _push_mode() -> str:
    # bash ${REBAR_PUSH:-always}: unset OR empty → "always" (the `:-` form), then
    # lowercase + strip whitespace.
    return "".join((os.environ.get("REBAR_PUSH") or "always").lower().split())


def _git(base: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", base, *args], capture_output=True, text=True, env=env
    )


def push_tickets_branch(base_path: str) -> None:
    """Push ``HEAD:tickets`` to origin per the REBAR_PUSH policy (best-effort)."""
    mode = _push_mode()
    if mode == "off":
        return
    if mode == "async":
        # Detach a synchronous push (REBAR_PUSH=always) that survives parent exit.
        child_env = {**os.environ, "REBAR_PUSH": "always"}
        try:
            subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; from rebar._store import push; push.push_tickets_branch(sys.argv[1])",
                 base_path],
                env=child_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
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
            print(f"Warning: tickets branch push failed (exit {res.returncode}): {stderr}", file=sys.stderr)
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
            base_path, "merge", "origin/tickets", "--no-edit",
            "-m", "Merge origin/tickets (auto-reconcile during push retry)",
        )
        if merge.returncode == 0:
            continue  # merged clean — retry push next iter

        if _DIRTY_WD.search(merge.stderr or ""):
            # Dirty working tree (e.g. reconciler .bridge_state/* files): stash → merge → pop.
            stash = _git(base_path, "stash", "push", "--quiet", "-m", "_push_tickets_branch:auto-stash")
            if stash.returncode != 0:
                print(f"Warning: tickets branch push failed: stash failed (attempt {attempt})", file=sys.stderr)
                continue
            merge2 = _git(base_path, "merge", "origin/tickets", "--no-edit",
                          "-m", "Merge origin/tickets (auto-reconcile, post-stash)")
            _git(base_path, "stash", "pop", "--quiet")  # pop unconditionally
            if merge2.returncode != 0:
                _git(base_path, "merge", "--abort")
                print(f"Warning: tickets branch merge failed after stash recovery (attempt {attempt})", file=sys.stderr)
            continue

        # Real content conflict — retry won't help, but continue so _MAX_RETRIES is honored.
        _git(base_path, "merge", "--abort")
        print(f"Warning: tickets branch push failed (merge conflict, attempt {attempt})", file=sys.stderr)

    print(f"Warning: tickets branch push failed after {_MAX_RETRIES} retries", file=sys.stderr)
