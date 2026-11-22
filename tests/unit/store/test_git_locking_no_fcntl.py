from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar._store import git_locking, lock_kernel


def _no_exclusive_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ``fcntl`` nor ``msvcrt``: the platform has no kernel-mediated lock at all."""
    monkeypatch.setattr(lock_kernel, "fcntl", None)
    monkeypatch.setattr(lock_kernel, "msvcrt", None)


def _fake_git_repo(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_store_git_op_lock_without_fcntl_executes_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / "tracker"
    _fake_git_repo(tracker)
    _no_exclusive_leg(monkeypatch)

    protected = tracker / "protected.txt"
    with git_locking._store_git_op_lock(str(tracker)):
        protected.write_text("ran\n", encoding="utf-8")

    assert protected.read_text(encoding="utf-8") == "ran\n"


def test_fetch_coordination_lock_without_fcntl_executes_unlocked_without_lock_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _fake_git_repo(repo)
    _no_exclusive_leg(monkeypatch)

    protected = repo / "protected.txt"
    with git_locking.fetch_coordination_lock(str(repo)):
        protected.write_text("ran\n", encoding="utf-8")

    assert protected.read_text(encoding="utf-8") == "ran\n"
    assert not (repo / ".git" / "rebar-fetch.lock").exists()


def test_acquire_fetch_coord_flock_without_fcntl_reports_unacquired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "rebar-fetch.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    _no_exclusive_leg(monkeypatch)
    try:
        assert git_locking._acquire_fetch_coord_flock(fd) is False
    finally:
        os.close(fd)
