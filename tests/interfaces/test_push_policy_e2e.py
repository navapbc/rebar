"""REBAR_PUSH auto-push policy, end-to-end against a real local bare origin.

In-process port of the real-origin half of tests/scripts/test-rebar-push-policy.sh
(the bash engine is being deleted). The unit tier already covers parsing + the
off / no-remote no-ops (tests/unit/test_store.py); this pins the observable git
effect under each mode:

  * off    — origin/tickets must NOT move.
  * always — origin/tickets advances synchronously, before create() returns.
  * async  — create() returns immediately; the push lands within a bounded wait.
  * ' OFF ' (case/space-insensitive) — must also disable the push.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """An initialized rebar repo wired to a real local bare origin.

    Yields (repo, origin_git_dir).
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo, origin


def _origin_ref(origin: Path) -> str:
    r = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/tickets"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or "NONE"


def test_push_off_does_not_move_origin(
    repo_with_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = repo_with_origin
    before = _origin_ref(origin)
    monkeypatch.setenv("REBAR_PUSH", "off")
    rebar.create_ticket("task", "off", repo_root=str(repo))
    assert _origin_ref(origin) == before


def test_push_always_moves_origin_synchronously(
    repo_with_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = repo_with_origin
    before = _origin_ref(origin)
    monkeypatch.setenv("REBAR_PUSH", "always")
    rebar.create_ticket("task", "always", repo_root=str(repo))
    # Synchronous: origin already advanced by the time create() returned.
    assert _origin_ref(origin) != before


def test_push_async_moves_origin_within_bounded_wait(
    repo_with_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = repo_with_origin
    before = _origin_ref(origin)
    monkeypatch.setenv("REBAR_PUSH", "async")
    rebar.create_ticket("task", "async", repo_root=str(repo))
    after = before
    for _ in range(25):  # ≤ ~10s, same bound as the bash test (25 × 0.4s)
        after = _origin_ref(origin)
        if after != before:
            break
        time.sleep(0.4)
    assert after != before, "REBAR_PUSH=async never pushed in the background"


def test_push_off_is_case_and_space_insensitive(
    repo_with_origin: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = repo_with_origin
    before = _origin_ref(origin)
    monkeypatch.setenv("REBAR_PUSH", " OFF ")
    rebar.create_ticket("task", "off2", repo_root=str(repo))
    assert _origin_ref(origin) == before
