"""Self-tests for the repo-isolation guard (tests/conftest.py + tests/_isolation.py).

The guard exists so a test that commits to — or dirties — the rebar checkout
fails loudly instead of silently polluting it (the failure mode that once leaked
dozens of ``ticket: link ...`` commits onto main). These tests prove the guard
actually fires, so the safety net itself can't rot unnoticed:

* the detection primitives spot a commit (HEAD move) and a stray working-tree
  file, and report ``None`` outside a repo;
* end-to-end via ``pytester``: an autouse HEAD guard fails a committing test, the
  session backstop flags a working-tree write, and a clean test passes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _isolation  # noqa: E402

pytest_plugins = ["pytester"]


def _init_repo(path: Path) -> None:
    """Make *path* a git repo with one seed commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "seed").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)


# ── detection primitives ──────────────────────────────────────────────────────


def test_head_detects_a_commit(tmp_path):
    _init_repo(tmp_path)
    before = _isolation.head(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-q", "-m", "leak"],
        check=True,
    )
    after = _isolation.head(tmp_path)
    assert before and after and before != after


def test_head_is_none_outside_a_repo(tmp_path):
    assert _isolation.head(tmp_path / "not-a-repo") is None


def test_porcelain_reports_a_new_working_tree_file(tmp_path):
    _init_repo(tmp_path)
    base = _isolation.porcelain(tmp_path)
    (tmp_path / "stray.txt").write_text("leak")
    after = _isolation.porcelain(tmp_path)
    assert base is not None and after is not None
    assert any("stray.txt" in line for line in after - base)


# ── end-to-end wiring (pytester) ──────────────────────────────────────────────

# An inline conftest that installs the same guard pattern as the real one, but
# pointed at a throwaway repo and built on the shared _isolation primitives, so
# the meaningful logic is not duplicated.
_INLINE_CONFTEST = """
import sys
sys.path.insert(0, {tests_dir!r})
from typing import Iterator

import pytest

import _isolation

_ROOT = {root!r}
_PORCELAIN_START = {{}}


@pytest.fixture(autouse=True)
def _no_repo_commits() -> Iterator[None]:
    before = _isolation.head(_ROOT)
    yield
    after = _isolation.head(_ROOT)
    if before is not None and after is not None and before != after:
        pytest.fail(f"Test moved the repo HEAD ({{before[:10]}} -> {{after[:10]}})")


def pytest_sessionstart(session):
    _PORCELAIN_START["v"] = _isolation.porcelain(_ROOT)


def pytest_sessionfinish(session, exitstatus):
    start = _PORCELAIN_START.get("v")
    after = _isolation.porcelain(_ROOT)
    if start is None or after is None:
        return
    if after - start:
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            "REPO ISOLATION FAILURE: " + ", ".join(sorted(after - start)), red=True
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
"""


def _write_inline_conftest(pytester, repo_root: Path) -> None:
    pytester.makeconftest(
        _INLINE_CONFTEST.format(tests_dir=str(_TESTS_DIR), root=str(repo_root))
    )


def test_guard_fails_a_test_that_commits(pytester, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_inline_conftest(pytester, repo)
    pytester.makepyfile(
        f"""
        import subprocess
        def test_leaks_a_commit():
            subprocess.run(
                ["git", "-C", {str(repo)!r}, "commit", "--allow-empty", "-q", "-m", "leak"],
                check=True,
            )
        """
    )
    result = pytester.runpytest()
    # The test body runs (and commits) — its call phase passes — then the guard's
    # post-yield teardown fails, which pytest reports as an ERROR. Either way the
    # run is non-zero and the offending test is named.
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*moved the repo HEAD*"])


def test_guard_passes_an_isolated_test(pytester, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_inline_conftest(pytester, repo)
    pytester.makepyfile(
        """
        def test_uses_only_tmp_path(tmp_path):
            (tmp_path / "scratch").write_text("fine")
            assert (tmp_path / "scratch").read_text() == "fine"
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert result.ret == 0


def test_session_backstop_flags_a_working_tree_write(pytester, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_inline_conftest(pytester, repo)
    pytester.makepyfile(
        f"""
        def test_writes_into_the_checkout():
            import pathlib
            (pathlib.Path({str(repo)!r}) / "leaked_into_tree.txt").write_text("oops")
        """
    )
    result = pytester.runpytest()
    # The test itself passes, but the session is escalated to a failure.
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*REPO ISOLATION FAILURE*"])
