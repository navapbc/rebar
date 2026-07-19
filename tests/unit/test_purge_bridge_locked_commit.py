"""Regression: ``purge-bridge`` commits deletions through the canonical locked write path.

Bug 4dd2. ``purge_bridge._commit_deletion`` used to commit ticket-directory removals with a
raw, UNLOCKED, WHOLE-INDEX write — ``git -C <tracker> add -A`` + an unscoped
``git commit --no-verify`` — taking neither the store write lock nor a commit pathspec. That
is a side-channel write (violates invariant I5, docs/concurrency.md): under a concurrent
locked writer it swept that writer's staged-but-uncommitted event blob into the purge commit
(sweep-and-strand data loss).

These tests pin the observable contract, not private names:

* ``_commit_deletion`` HOLDS the store write lock while it runs the git commit.
* it commits ONLY the deleted ticket-dir pathspecs, so a concurrent writer's staged blob is
  never swept into the purge commit (and stays staged, exactly as the writer left it).
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

import pytest

from rebar._commands import purge_bridge
from rebar._store import lock as lockmod

pytestmark = pytest.mark.unit


def _git(d, *a, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    td = tmp_path / "trk"
    td.mkdir()
    _git(td, "init", "-q", "-b", "tickets")
    _git(td, "config", "user.email", "t@e.com")
    _git(td, "config", "user.name", "T")
    (td / "seed").write_text("seed\n")
    _git(td, "add", "-A")
    _git(td, "commit", "-q", "-m", "seed")
    return str(td)


def _committed_jira_dir(tracker: str, name: str = "jira-FOO-1") -> Path:
    """Create + commit a jira-* ticket dir, then rmtree it (as purge_bridge_cli does)."""
    tdir = Path(tracker) / name
    tdir.mkdir()
    (tdir / "0001-CREATE.json").write_text('{"data": {"jira_key": "FOO-1"}}')
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", f"add {name}")
    shutil.rmtree(tdir)
    return tdir


def test_commit_deletion_holds_write_lock(tracker, monkeypatch):
    """The commit must run while the store write lock is HELD (I5)."""
    tdir = _committed_jira_dir(tracker)

    lock_held = {"v": False}
    real_write_lock = lockmod.write_lock

    @contextlib.contextmanager
    def spy_write_lock(*a, **kw):
        with real_write_lock(*a, **kw):
            lock_held["v"] = True
            try:
                yield
            finally:
                lock_held["v"] = False

    commit_saw_lock = {"v": None}
    real_run = subprocess.run

    def spy_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd:
            commit_saw_lock["v"] = lock_held["v"]
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(lockmod, "write_lock", spy_write_lock)
    monkeypatch.setattr(purge_bridge.subprocess, "run", spy_run)

    purge_bridge._commit_deletion(tracker, [str(tdir)], 1, "BAR")

    # The lock was held at the exact moment the deletion was committed.
    assert commit_saw_lock["v"] is True


def test_commit_deletion_does_not_sweep_staged_blob(tracker):
    """A concurrent writer's staged blob must NOT be swept into the purge commit."""
    tdir = _committed_jira_dir(tracker)

    # Simulate a concurrent locked writer that has staged (git add) an event but not committed.
    foreign = Path(tracker) / "other" / "foreign.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("{}")
    _git(tracker, "add", "--", "other/foreign.json")

    purge_bridge._commit_deletion(tracker, [str(tdir)], 1, "BAR")

    head_paths = _git(tracker, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout
    # The purge commit contains the deletion...
    assert "jira-FOO-1/0001-CREATE.json" in head_paths
    # ...and NOTHING else — the concurrent writer's staged blob was not swept in.
    assert "other/foreign.json" not in head_paths
    # The foreign blob is still staged (uncommitted), exactly as the writer left it.
    assert "other/foreign.json" in _git(tracker, "diff", "--cached", "--name-only").stdout
