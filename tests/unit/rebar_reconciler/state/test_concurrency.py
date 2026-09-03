"""Unit tests for _concurrency.py's remaining snapshot-head contract."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"


def _load_module() -> ModuleType:
    import sys

    spec = importlib.util.spec_from_file_location("_concurrency", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so that dataclass annotation resolution
    # (which calls sys.modules.get(cls.__module__)) works in Python 3.14+.
    sys.modules["_concurrency"] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop("_concurrency", None)
        raise
    return mod


@pytest.fixture(scope="module")
def concurrency() -> ModuleType:
    """Return the _concurrency module; fail all tests if absent."""
    if not MODULE_PATH.exists():
        pytest.fail(
            f"_concurrency.py not found at {MODULE_PATH} — implement the module to make tests pass."
        )
    return _load_module()


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


def test_snapshot_head_returns_nonempty_string(concurrency, tmp_git_repo: Path) -> None:
    """snapshot_head() on a real tmp git repo returns a non-empty hex string."""
    sha = concurrency.snapshot_head(tmp_git_repo)
    assert isinstance(sha, str)
    assert len(sha) > 0
    # Should look like a hex SHA (at least 7 chars)
    assert all(c in "0123456789abcdef" for c in sha.lower())


def test_snapshot_head_returns_sentinel_on_empty_repo(concurrency, tmp_path: Path) -> None:
    """F9 regression: snapshot_head must return EMPTY_REPO_SENTINEL on a bare
    repo (``git init`` with no commits), not raise CalledProcessError.

    Before F9, the second subprocess.run used check=True; on a bare repo where
    neither ``tickets`` nor ``HEAD`` resolves, the call raised and the
    reconciler could not bootstrap. The fix returns a sentinel that drift
    detection treats as stable until the first commit lands.
    """
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

    # Must NOT raise
    sha = concurrency.snapshot_head(tmp_path)
    assert sha == concurrency.EMPTY_REPO_SENTINEL, (
        f"snapshot_head on a bare repo must return EMPTY_REPO_SENTINEL; got {sha!r}"
    )


def test_snapshot_module_keeps_only_snapshot_head_contract(concurrency) -> None:
    assert not hasattr(concurrency, "ConcurrencyEvent")
    assert not hasattr(concurrency, "Result")
    assert not hasattr(concurrency, "rebase_retry")
