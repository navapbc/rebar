"""The projects.json seed looks the file up by a git path, which always uses ``/``.

``_PROJECTS_REL_PATH`` was built with ``os.path.join``, so on Windows the seed asked git for
``tickets:.bridge_state\\projects.json``, never found the committed file, and rewrote it on
every ensure sweep, adding processes to every op-cert job (bug undamaging-ashamed-wasp).
"""

from __future__ import annotations

import importlib
import ntpath
import os
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._store import project_ensures

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env(),
    ).stdout.strip()


@pytest.fixture
def tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "tracker"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")
    _git(repo, "config", "user.email", "wasp@example.com")
    _git(repo, "config", "user.name", "wasp")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    monkeypatch.setattr(project_ensures, "_effective_project", lambda: "")
    return repo


def test_the_git_lookup_path_uses_forward_slashes_even_with_windows_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-import the module with Windows path joining, as it runs on Windows."""
    monkeypatch.setattr(os.path, "join", ntpath.join)
    try:
        reloaded = importlib.reload(project_ensures)
        rel_path = reloaded._PROJECTS_REL_PATH
    finally:
        monkeypatch.undo()
        importlib.reload(project_ensures)
    assert rel_path == ".bridge_state/projects.json"


def test_a_second_sweep_finds_the_committed_mapping_and_writes_nothing(tracker: Path) -> None:
    first = project_ensures.seed_projects_mapping_unit(str(tracker))
    assert first.status == "changed"
    seeded = tracker / ".bridge_state" / "projects.json"
    head = _git(tracker, "rev-parse", "tickets")
    mtime = seeded.stat().st_mtime_ns

    second = project_ensures.seed_projects_mapping_unit(str(tracker))

    assert second.status == "ok", second
    assert _git(tracker, "rev-parse", "tickets") == head
    assert seeded.stat().st_mtime_ns == mtime
