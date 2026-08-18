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

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _isolation  # noqa: E402
from _subprocess_env import subprocess_env  # noqa: E402

pytest_plugins = ["pytester"]


def _blocking_git(tmp_path: Path, operation: str) -> tuple[Path, dict[str, str]]:
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / f"{operation}.count"
    shim = bin_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "operation = os.environ['REBAR_BLOCK_GIT_OPERATION']\n"
        "tail = ['status', '--porcelain'] if operation == 'status' else "
        "['rev-parse', 'HEAD']\n"
        "if args[-2:] == tail:\n"
        "    state = Path(os.environ['REBAR_BLOCK_GIT_STATE'])\n"
        "    count = int(state.read_text()) if state.exists() else 0\n"
        "    state.write_text(str(count + 1))\n"
        "    if count + 1 == 2:\n"
        "        os.close(1)\n"
        "        os.close(2)\n"
        "        time.sleep(60)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *args])\n"
    )
    shim.chmod(0o755)
    env = subprocess_env()
    env["PATH"] = os.pathsep.join((str(bin_dir), env["PATH"]))
    env["PYTHONPATH"] = os.pathsep.join((str(_TESTS_DIR), env.get("PYTHONPATH", "")))
    env["REBAR_BLOCK_GIT_OPERATION"] = operation
    env["REBAR_BLOCK_GIT_STATE"] = str(state)
    return state, env


def _run_real_guard_with_blocking_git(
    tmp_path: Path, operation: str
) -> subprocess.CompletedProcess:
    state, env = _blocking_git(tmp_path, operation)
    nested_test = tmp_path / "test_nested_guard.py"
    nested_test.write_text("def test_body_finishes():\n    assert True\n")
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "conftest",
                "--basetemp",
                str(tmp_path / "nested-pytest"),
                str(nested_test),
            ],
            cwd=_TESTS_DIR.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"real repo-isolation {operation} probe kept pytest alive beyond 12 seconds"
        ) from exc
    elapsed = time.monotonic() - started
    assert state.read_text() == "2", "the shim did not block the second real guard probe"
    # timing: hang-guard — the 10s ceiling only proves the inner 5s watchdog beats 60s sleep.
    assert elapsed < 10, f"watchdog did not bound the blocked probe: {elapsed:.2f}s"
    return result


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


def test_session_finish_fails_explicitly_when_porcelain_times_out(tmp_path):
    result = _run_real_guard_with_blocking_git(tmp_path, "status")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "TimeoutExpired" in output, output


def test_commit_guard_fails_explicitly_when_head_times_out(tmp_path):
    result = _run_real_guard_with_blocking_git(tmp_path, "head")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "TimeoutExpired" in output, output


def test_git_probe_timeout_ignores_a_test_monkeypatch_of_time_sleep(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    state, env = _blocking_git(tmp_path, "head")
    for key in (
        "PATH",
        "PYTHONPATH",
        "REBAR_BLOCK_GIT_OPERATION",
        "REBAR_BLOCK_GIT_STATE",
    ):
        monkeypatch.setenv(key, env[key])
    assert _isolation.head(repo)
    assert state.read_text() == "1"
    monkeypatch.setattr(_isolation, "GIT_PROBE_TIMEOUT_SECONDS", 0.05)
    sleep_calls: list[float] = []

    def fail_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        pytest.fail("repo-isolation probe used the test's patched sleep")

    monkeypatch.setattr(time, "sleep", fail_sleep)
    assert subprocess.time.sleep is fail_sleep

    with pytest.raises(subprocess.TimeoutExpired):
        _isolation.head(repo)
    assert sleep_calls == []


def test_leak_snapshot_catches_new_top_level_entry(tmp_path):
    before = _isolation.repo_leak_snapshot(tmp_path)
    (tmp_path / "depends_on").mkdir()  # the classic relative-path leak shape
    leaked = _isolation.repo_leak_snapshot(tmp_path) - before
    assert "depends_on" in leaked, leaked


def test_leak_snapshot_catches_write_into_preexisting_state_dir(tmp_path):
    """Regression for hurt-brow-swan: a leak INTO a pre-existing watched dir
    (``.rebar/``) must be detected locally, even though it adds no new top-level
    entry — the exact blind spot of a top-level-only ``os.listdir`` diff."""
    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir()
    (rebar_dir / "current_session_log").write_text("pre-existing")  # dir pre-exists
    before = _isolation.repo_leak_snapshot(tmp_path)
    (rebar_dir / "run_snapshots").mkdir()  # the leak — inside the pre-existing dir
    leaked = _isolation.repo_leak_snapshot(tmp_path) - before
    assert ".rebar/run_snapshots" in leaked, leaked


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


@pytest.fixture(autouse=True)
def _no_repo_root_leaks() -> Iterator[None]:
    before = _isolation.repo_leak_snapshot(_ROOT)
    yield
    leaked = _isolation.repo_leak_snapshot(_ROOT) - before
    if leaked:
        pytest.fail(f"Test leaked into REPO_ROOT: {{sorted(leaked)}}")


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
    pytester.makeconftest(_INLINE_CONFTEST.format(tests_dir=str(_TESTS_DIR), root=str(repo_root)))


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


def test_leak_guard_fails_a_test_that_writes_into_preexisting_state_dir(pytester, tmp_path):
    """End-to-end (hurt-brow-swan): the per-test leak guard must fail a test that
    writes INTO a pre-existing ``.rebar/`` — locally, with no fresh CI checkout."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".rebar").mkdir()  # pre-exists, as it always does in a dogfooding checkout
    (repo / ".rebar" / "current_session_log").write_text("pre-existing")
    _write_inline_conftest(pytester, repo)
    pytester.makepyfile(
        f"""
        def test_leaks_into_rebar():
            import pathlib
            (pathlib.Path({str(repo)!r}) / ".rebar" / "leaked.txt").write_text("oops")
        """
    )
    result = pytester.runpytest()
    # Call phase passes; the post-yield teardown fails -> reported as an ERROR.
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*leaked into REPO_ROOT*.rebar/leaked.txt*"])
