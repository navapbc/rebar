"""Share process-group reaping and detached-child spawning across rebar.

Grounding and ACLI callers supply their own grace, drain, and log identity.
Async push and background workers share a durable cwd and import bootstrap.
This stdlib-only leaf remains importable by path-loaded reconciler code.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import IO


def reap_process_group(
    proc: subprocess.Popen[str],
    *,
    grace: float,
    drain: float,
    label: str,
    logger: logging.Logger,
) -> None:
    """Reap a timed-out process group and pipe-holding descendants.

    On POSIX, signal TERM, allow ``grace``, then KILL and bound the final ``drain``;
    log survivors with the caller's label. Handle exited-group races. Other
    platforms kill the child and wait within the supplied bounds.
    """
    if os.name != "posix":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace + drain)
        except subprocess.TimeoutExpired:
            logger.warning("%s child PID %s did not exit after kill (leaked)", label, proc.pid)
        return

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # Child already gone (ESRCH) or we can't see it — best-effort reap and return.
        try:
            proc.wait(timeout=drain)
        except subprocess.TimeoutExpired:
            pass
        return

    # SIGTERM the group, then give it a grace window to flush + exit cleanly.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=grace)
        return  # exited on SIGTERM within the grace window — drained.
    except subprocess.TimeoutExpired:
        pass

    # Grace expired — SIGKILL the group, then bound the final reap/drain so a
    # D-state child cannot hang us indefinitely.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=drain)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s process group %s survived SIGKILL after %ss drain (leaked PID %s)",
            label,
            pgid,
            drain,
            proc.pid,
        )


def store_repo_root(tracker: str | os.PathLike[str]) -> str:
    """Resolve the store repo root from the tracker's real path.

    Symlink resolution avoids ephemeral worktree parents. This is the store root,
    not the code/config root; callers needing ``rebar.toml`` resolve that separately.
    A relative path from a removed cwd may raise ``OSError``.
    """
    # repo-root-seam: THE sanctioned store-repo-root derivation; every other site routes here.
    return os.path.dirname(os.path.realpath(tracker))


def detached_child_cwd(tracker: str) -> str:
    """Anchor a detached child outside its possibly short-lived caller worktree.

    Use the canonical store repo root, then its nearest existing ancestor; never
    the tracker itself. A dead cwd or other path failure falls back to filesystem
    root without failing the triggering write.
    """
    try:
        root = store_repo_root(tracker)
    except OSError:
        return os.sep
    while root and not os.path.isdir(root):
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return root if root and os.path.isdir(root) else os.sep


def _detach_kwargs() -> dict:
    """Platform detach kwargs. POSIX: a new session so the child outlives the parent. Windows
    (authored, API-derisked; NOT reached in v1 — every caller no-ops on nt before spawning):
    DETACHED_PROCESS | CREATE_NO_WINDOW (constants exist only on Windows, referenced only
    inside this branch)."""
    if os.name == "nt":  # pragma: no cover - POSIX CI
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def spawn_detached(
    module: str,
    func: str,
    *args: str,
    env: dict[str, str],
    stderr: int | IO[str],
) -> None:
    """Spawn a detached Python child with durable cwd and import path.

    Copy the caller's env before adding PYTHONPATH; the caller supplies stderr and
    handles any ``Popen`` failure. ``args[0]`` must be a canonical store path for
    the cwd anchor. Arguments remain strings on the child's argv.
    """
    if not args:
        raise ValueError("spawn_detached needs at least one argument for the child")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_env = dict(env)
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    n = len(args)
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, sys.argv[{n + 1}]); "
            f"import {module}; {module}.{func}(*sys.argv[1:{n + 1}])",
            *args,
            src,
        ],
        cwd=detached_child_cwd(args[0]),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        **_detach_kwargs(),
    )
