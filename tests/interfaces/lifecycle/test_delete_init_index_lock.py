"""The delete and init store-write paths self-heal a git ``index.lock`` too
(sibling ticket 3b4e — every ticket-store committer must route through the
index.lock-resilient ``gitutil.run_git_write``, with a regression test per path).

Companion to ``test_compact_index_lock.py`` (which covers the compact path). Here we
pin the ``delete.py`` and ``init.py`` write paths: a provably-stale ``index.lock`` on
the tracker is reclaimed and the write succeeds, instead of failing hard.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import rebar
from rebar import config
from rebar._commands import delete as _delete
from rebar._commands import init as _init
from rebar._store import gitutil

_STALE_S = getattr(gitutil, "_INDEX_LOCK_STALE_S", 300)


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(config.tracker_dir(str(repo)))


def _plant_stale_lock(tracker: str) -> Path:
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    lock = Path(p) if os.path.isabs(p) else Path(tracker) / p
    lock.write_text("")  # leftover lock from a crashed git process
    old = time.time() - (_STALE_S + 60)
    os.utime(lock, (old, old))
    return lock


def test_delete_reclaims_stale_index_lock(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "del")
    tid = rebar.create_ticket("task", "doomed", repo_root=str(repo))
    lock = _plant_stale_lock(_tracker(repo))

    rc = _delete.delete_cli([tid, "--user-approved"], repo_root=str(repo))
    assert rc == 0, "delete must self-heal a stale index.lock, not fail hard"
    assert not lock.exists(), "the stale lock should have been reclaimed by the delete write"
    # the ticket is tombstoned (its .tombstone.json marker was committed)
    assert (Path(_tracker(repo)) / tid / ".tombstone.json").is_file()


def test_init_git_commit_reclaims_stale_index_lock(tmp_path: Path) -> None:
    """init.py's ``_git`` shim (which routes through ``run_git_write``) self-heals a stale
    index.lock on an index-mutating commit — the same resilience the shared wrapper gives
    the event-append/transition/compact/delete paths."""
    repo = _init_repo(tmp_path, "ini")
    tracker = _tracker(repo)
    # stage a change so there is something to commit through init._git
    (Path(tracker) / ".init-lock-probe").write_text("x")
    _init._git(tracker, "add", "--", ".init-lock-probe")
    lock = _plant_stale_lock(tracker)

    cp = _init._git(tracker, "commit", "-q", "--no-verify", "-m", "probe: init _git self-heal")
    assert cp.returncode == 0, "init._git must self-heal a stale index.lock, not fail hard"
    assert not lock.exists(), "the stale lock should have been reclaimed by init._git"
