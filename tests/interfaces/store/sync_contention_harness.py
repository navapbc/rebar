"""Shared harness for read integrity under sync contention.

``_rebar_cli`` exercises the consumer-facing path with background pushes.
``_clear_sync_throttle`` removes the per-store marker so each read reaches
``ensure_fresh`` instead of its one-minute shortcut. ``build_repo_with_origin_tickets``
supplies the bare-origin topology used by the bare-import fixture in ``conftest.py``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

import rebar


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rebar_cli(*args: str, repo: Path, push: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke the real `rebar` CLI in a subprocess (the consumer-facing path), with
    REBAR_SYNC_PUSH set so background pushes contend exactly as the bug describes."""
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
    env["REBAR_SYNC_PUSH"] = push
    rebar_bin = shutil.which("rebar")
    cmd = [rebar_bin, *args] if rebar_bin else [sys.executable, "-m", "rebar", *args]
    return subprocess.run(
        cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _clear_sync_throttle(tracker: Path) -> None:
    tracker_abs = os.path.realpath(str(tracker))
    md5_12 = hashlib.md5(tracker_abs.encode()).hexdigest()[:12]
    marker = f"/tmp/.ticket-sync-{md5_12}"
    try:
        os.unlink(marker)
    except OSError:
        pass


def build_repo_with_origin_tickets(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repo whose tracker has an `origin/tickets` upstream, so `ensure_fresh`
    actually reconverges (it early-returns when there's no remote branch).
    Returns (repo_path, tracker_path, ticket_id). Caller must have
    REBAR_SYNC_PUSH=off in the environment while building."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "no-stall target", repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    _git("push", "-q", "origin", "tickets:tickets", cwd=tracker)
    return repo, tracker, tid
