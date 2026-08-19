"""Happy-path oracle for the locked commit-and-push store entrypoint."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import push

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed


def _bare_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed


@pytest.fixture
def tracker_and_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.name", "Ambient Author")
    _git(tracker, "config", "user.email", "ambient@example.com")
    _git(tracker, "remote", "add", "origin", str(origin))
    (tracker / "seed.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(
        tracker,
        "fetch",
        "-q",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    return tracker, origin


def test_dirty_tracker_is_committed_and_delivered(
    tracker_and_origin: tuple[Path, Path],
) -> None:
    tracker, origin = tracker_and_origin
    event = tracker / "dirty-event.json"
    event.write_text('{"event": "inbound"}\n', encoding="utf-8")
    assert "dirty-event.json" in _git(tracker, "status", "--porcelain").stdout

    push.commit_and_push_tickets_branch(
        tracker,
        message="chore: commit inbound bridge events",
        strict=True,
    )

    local_head = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    remote_head = _bare_git(origin, "rev-parse", "tickets").stdout.strip()
    assert remote_head == local_head
    assert _git(tracker, "status", "--porcelain").stdout == ""
    assert _bare_git(origin, "show", "tickets:dirty-event.json").stdout == event.read_text()
    assert _git(tracker, "log", "-1", "--format=%s").stdout.strip() == (
        "chore: commit inbound bridge events"
    )
