"""Fixtures for the interface-parity tier.

These tests exercise the three rebar interfaces (Python library, CLI, MCP) over
ONE git-backed ticket store, asserting they behave identically. The tier is
intentionally outside the unit/scripts network guard (it subprocesses git, no
network) and uses a real temp git repo per test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar

# Make this directory importable from subdirectory tests. pytest's prepend
# import mode puts each test file's own dir on sys.path, but the seam
# subdirectories under tests/interfaces/ do not contain ``adapters.py`` — it
# lives here at the interfaces root. A parent-dir conftest is imported before
# descending into subdirs, so this insertion runs before the subdir test
# modules load, keeping ``from adapters import ...`` resolvable everywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def rebar_repo(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An initialized rebar repo in a temp git dir.

    Sets REBAR_ROOT to the repo
    so the no-repo-root-leak guard never fires on bridge_state/.tickets-tracker
    writes, and initializes the ticket system. Yields the repo path.
    """
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    yield repo
