from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar._store import git_locking


def _fake_git_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_store_git_op_lock_without_fcntl_executes_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / "tracker"
    _fake_git_repo(tracker)
    monkeypatch.setattr(git_locking, "fcntl", None)

    protected = tracker / "protected.txt"
    with git_locking._store_git_op_lock(str(tracker)):
        protected.write_text("ran\n")

    assert protected.read_text() == "ran\n"


def test_fetch_coordination_lock_without_fcntl_executes_unlocked_without_lock_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _fake_git_repo(repo)
    monkeypatch.setattr(git_locking, "fcntl", None)

    protected = repo / "protected.txt"
    with git_locking.fetch_coordination_lock(str(repo)):
        protected.write_text("ran\n")

    assert protected.read_text() == "ran\n"
    assert not (repo / ".git" / "rebar-fetch.lock").exists()


def test_acquire_fetch_coord_flock_without_fcntl_reports_unacquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "rebar-fetch.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(git_locking, "fcntl", None)
    try:
        assert git_locking._acquire_fetch_coord_flock(fd) is False
    finally:
        os.close(fd)
