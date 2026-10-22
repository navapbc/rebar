"""Unit tests for ``applier.snapshot_head``, the tickets-branch HEAD read the
batch applier's drift guard pins and re-checks."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def applier() -> ModuleType:
    return importlib.import_module("rebar.reconciler.applier")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with one commit and return its root."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_snapshot_head_returns_nonempty_string(applier, tmp_git_repo: Path) -> None:
    """snapshot_head() on a real tmp git repo returns a non-empty hex string."""
    sha = applier.snapshot_head(tmp_git_repo)
    assert isinstance(sha, str)
    assert len(sha) > 0
    # Should look like a hex SHA (at least 7 chars)
    assert all(c in "0123456789abcdef" for c in sha.lower())


def test_snapshot_head_returns_sentinel_on_empty_repo(applier, tmp_path: Path) -> None:
    """F9 regression: snapshot_head must return EMPTY_REPO_SENTINEL on a bare
    repo (``git init`` with no commits), not raise CalledProcessError.

    Before F9, the second subprocess.run used check=True; on a bare repo where
    neither ``tickets`` nor ``HEAD`` resolves, the call raised and the
    reconciler could not bootstrap. The fix returns a sentinel that drift
    detection treats as stable until the first commit lands.
    """
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

    # Must NOT raise
    sha = applier.snapshot_head(tmp_path)
    assert sha == applier.EMPTY_REPO_SENTINEL, (
        f"snapshot_head on a bare repo must return EMPTY_REPO_SENTINEL; got {sha!r}"
    )


def test_concurrency_module_is_gone() -> None:
    """``snapshot_head`` lives in ``applier``; the old ``_concurrency`` module is deleted."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("rebar.reconciler._concurrency")
