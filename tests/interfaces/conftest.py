"""Fixtures for the interface-parity tier.

These tests exercise the three rebar interfaces (Python library, CLI, MCP) over
ONE git-backed ticket store, asserting they behave identically. The tier is
intentionally outside the unit/scripts network guard (it subprocesses git, no
network) and uses a real temp git repo per test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest

import rebar


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def rebar_repo(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir.

    Sets both REBAR_ROOT and PROJECT_ROOT to the repo (the engine mirrors them)
    so the no-repo-root-leak guard never fires on bridge_state/.tickets-tracker
    writes, and initializes the ticket system. Yields the repo path.
    """
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo
