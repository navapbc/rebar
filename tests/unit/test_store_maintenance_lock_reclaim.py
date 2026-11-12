"""A stranded ``objects/maintenance.lock`` cannot disable store maintenance forever.

Git takes the lock before evaluating any task and, when it cannot, exits 0 without doing
anything; it never expires the lock. A hard kill during the explicit post-write step
therefore turned maintenance off for good. The step reclaims a lock older than git's own
``gc.pid`` staleness rule (12 h) and leaves a younger one for its holder.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from rebar._store import event_commit_git

pytestmark = pytest.mark.unit


@pytest.fixture
def loose_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with more loose objects than ``loose-objects``' auto threshold (100)."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    for name in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin-paths"],
        input="".join(f"{_blob(tmp_path, i)}\n" for i in range(150)),
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def _blob(tmp_path: Path, i: int) -> str:
    path = tmp_path / f"b{i}"
    path.write_text(f"blob {i}\n")
    return str(path)


def _in_pack(repo: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "count-objects", "-v"], check=True, capture_output=True, text=True
    ).stdout
    return int(dict(line.split(": ") for line in out.splitlines())["in-pack"])


def _plant_lock(repo: Path, age_s: float) -> Path:
    lock = repo / ".git" / "objects" / "maintenance.lock"
    lock.touch()
    then = time.time() - age_s
    os.utime(lock, (then, then))
    return lock


def test_stale_lock_is_reclaimed_and_maintenance_resumes(loose_repo: Path) -> None:
    lock = _plant_lock(loose_repo, age_s=13 * 3600)

    result = event_commit_git.run_auto_maintenance(loose_repo)

    assert result is not None and result.returncode == 0, result
    assert not lock.exists()
    assert _in_pack(loose_repo) >= 150, "maintenance must have packed the loose objects"


def test_young_lock_is_left_for_its_holder(loose_repo: Path) -> None:
    lock = _plant_lock(loose_repo, age_s=60)

    event_commit_git.run_auto_maintenance(loose_repo)

    assert lock.exists(), "a lock a live maintenance run may hold must never be removed"
    assert _in_pack(loose_repo) == 0
