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
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _isolation  # noqa: E402
from _nested_pytest import run_nested_pytest  # noqa: E402
from _subprocess_env import subprocess_env  # noqa: E402

pytest_plugins = ["pytester"]


def _blocking_git(
    tmp_path: Path, operation: str, *, persistent: bool = True
) -> tuple[Path, dict[str, str]]:
    """A ``git`` shim that stalls the guard's probe.

    ``persistent`` stalls EVERY probe from the second call onward, so the retry budget is
    exhausted and the guard must still fail loudly. ``persistent=False`` stalls exactly ONE
    call — the transient contention that bug 860b-28eb-10c0-4249 turned into a false red.
    """
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
        f"    if count + 1 {'>=' if persistent else '=='} 2:\n"
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
    try:
        result = run_nested_pytest(
            tmp_path,
            "-q",
            "-p",
            "conftest",
            str(nested_test),
            env=env,
            timeout=90,
            cwd=_TESTS_DIR.parent,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"real repo-isolation {operation} probe kept pytest alive beyond 90 seconds"
        ) from exc
    assert int(state.read_text()) >= 2, "the shim did not block the real guard probe"
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


def test_a_transient_probe_stall_is_re_sampled_rather_than_reported(tmp_path, monkeypatch):
    """One starved sample is contention, not a HEAD move (bug 860b-28eb-10c0-4249).

    The macOS full-suite sweep runs this probe once per test, three xdist workers deep, on
    a runner where ORDINARY fixture setup was measured at 4.562s against the 5s per-attempt
    bound. A single stalled sample there errored the innocent test that happened to be
    running and reddened the branch head. The probe is read-only and idempotent, so a
    transient stall must be re-sampled rather than reported.

    The paired guarantee — that a PERSISTENT stall still fails loudly — is held by
    ``test_commit_guard_fails_explicitly_when_head_times_out`` above.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    state, env = _blocking_git(tmp_path, "head", persistent=False)

    with monkeypatch.context() as patch:
        for key in (
            "PATH",
            "PYTHONPATH",
            "REBAR_BLOCK_GIT_OPERATION",
            "REBAR_BLOCK_GIT_STATE",
        ):
            patch.setenv(key, env[key])
        patch.setattr(_isolation, "GIT_PROBE_TIMEOUT_SECONDS", 0.5)
        before = _isolation.head(repo)
        assert before, "the first sample must succeed"
        after = _isolation.head(repo)  # the shim stalls this one sample

    assert after == before, "a transient stall must not be reported as a changed HEAD"
    assert int(state.read_text()) == 3, (
        "the stalled sample must be re-sampled, not reported; git was invoked "
        f"{state.read_text()} times"
    )


def test_git_probe_timeout_ignores_a_test_monkeypatch_of_time_sleep(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    state, env = _blocking_git(tmp_path, "head")
    original_timeout = _isolation.GIT_PROBE_TIMEOUT_SECONDS
    original_path = os.environ.get("PATH")
    sleep_calls: list[float] = []

    def fail_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        pytest.fail("repo-isolation probe used the test's patched sleep")

    with monkeypatch.context() as patch:
        for key in (
            "PATH",
            "PYTHONPATH",
            "REBAR_BLOCK_GIT_OPERATION",
            "REBAR_BLOCK_GIT_STATE",
        ):
            patch.setenv(key, env[key])
        assert _isolation.head(repo)
        assert state.read_text() == "1"
        patch.setattr(_isolation, "GIT_PROBE_TIMEOUT_SECONDS", 0.05)
        patch.setattr(time, "sleep", fail_sleep)
        assert subprocess.time.sleep is fail_sleep

        with pytest.raises(subprocess.TimeoutExpired):
            _isolation.head(repo)
    assert sleep_calls == []
    assert _isolation.GIT_PROBE_TIMEOUT_SECONDS == original_timeout
    assert os.environ.get("PATH") == original_path


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


# ── the guard must not delete what it cannot attribute (bug 746c) ─────────────
#
# The guard diffs REPO_ROOT around each test, so "appeared during this test" is
# all it can ever know — a concurrent write from another process in the same
# checkout produces an identical diff entry (reproduced 1/1 with a bare `touch`
# 8s into a 12s test body). Reporting such an entry is conservative: it costs a
# re-run. REMOVING it is not: it destroys another process's file, irreversibly
# and, for a directory, silently (`rmtree(..., ignore_errors=True)`).


def _root_conftest(request: pytest.FixtureRequest):
    """The live root-conftest plugin object (tests/conftest.py).

    Taken from the plugin manager rather than imported: ``sys.modules["conftest"]``
    is whichever conftest was imported last (the unit tier's), and re-importing the
    file by path would re-run its import-time setup inside a running session.
    """
    for name, plugin in request.config.pluginmanager.list_name_plugin():
        if name.endswith("tests/conftest.py") and hasattr(plugin, "_no_repo_root_leaks"):
            return plugin
    raise AssertionError("root conftest plugin not registered")


def _drive_leak_guard(request: pytest.FixtureRequest, root: Path, during: Callable[[], None]):
    """Run the REAL ``_no_repo_root_leaks`` against *root*, returning its failure.

    Drives the fixture generator by hand: the first ``next()`` takes the
    before-snapshot, *during* stands in for whatever appeared under REPO_ROOT while
    the test body ran, and the second ``next()`` runs the teardown under test.
    """
    conftest = _root_conftest(request)
    guard = getattr(conftest._no_repo_root_leaks, "__wrapped__", conftest._no_repo_root_leaks)
    with patch.object(conftest, "_REPO_ROOT", root):
        generator = guard()
        next(generator)
        during()
        with pytest.raises(pytest.fail.Exception) as failure:
            next(generator)
    return str(failure.value)


class _UnmarkedNode:
    """Stands in for a test node carrying no ``allow_repo_writes`` opt-out."""

    def get_closest_marker(self, name: str) -> None:
        return None


class _UnmarkedRequest:
    node = _UnmarkedNode()


def _drive_head_move_guard(request: pytest.FixtureRequest, root: Path, during: Callable[[], None]):
    """Run the REAL ``_no_repo_commits`` against *root*, returning its failure text.

    Same hand-driven shape as ``_drive_leak_guard``: the first ``next()`` samples
    HEAD, *during* stands in for whatever moved it while the test body ran, and the
    second ``next()`` runs the teardown under test.
    """
    conftest = _root_conftest(request)
    guard = getattr(conftest._no_repo_commits, "__wrapped__", conftest._no_repo_commits)
    with patch.object(conftest, "_REPO_ROOT", root):
        generator = guard(_UnmarkedRequest())
        next(generator)
        during()
        with pytest.raises(pytest.fail.Exception) as failure:
            next(generator)
    return str(failure.value)


def test_head_move_guard_warns_before_it_offers_the_destructive_recovery(request, tmp_path):
    """`git reset --hard` is correct for whoever owns the commit and destructive for
    everyone else, and the guard cannot tell them apart — this checkout hosts many
    worktrees and concurrent sessions. So the warning has to be readable *before* the
    command, not after it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    before = _isolation.head(repo)
    assert before is not None

    def another_process_commits() -> None:
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "not yours"],
            check=True,
        )

    message = _drive_head_move_guard(request, repo, another_process_commits)

    # The guard still fires, and still names the move.
    assert before[:10] in message
    # The recovery is still offered — it is right for whoever owns the commit.
    recovery = f"git reset --hard {before[:10]}"
    assert recovery in message
    lowered = message.lower()
    # ...but the reader is warned off it first, and told the external cause exists.
    assert "do not" in lowered, message
    assert lowered.index("do not") < lowered.index(recovery.lower()), message
    assert "concurrent" in lowered, message
    # And it never asserts the cause it cannot know.
    assert "committed into the rebar checkout instead of" not in message


def test_leak_guard_leaves_entries_it_cannot_attribute_on_disk(request, tmp_path):
    """A concurrent write from outside the suite must survive the guard's teardown.

    Covers both removal branches — the file ``unlink()`` and the directory
    ``rmtree()`` — since the guard cannot attribute either one.
    """
    stray_file = tmp_path / "EXTERNAL_MUTATION_PROBE"
    stray_dir = tmp_path / "external_dir_probe"

    def concurrent_writer() -> None:
        stray_file.write_text("written by another process in this checkout")
        stray_dir.mkdir()
        (stray_dir / "payload.txt").write_text("another process's data")

    _drive_leak_guard(request, tmp_path, concurrent_writer)

    assert stray_file.is_file(), "the guard deleted a file it cannot attribute to the test"
    assert stray_dir.is_dir(), "the guard deleted a directory it cannot attribute to the test"
    assert (stray_dir / "payload.txt").read_text() == "another process's data"


def test_leak_guard_still_fails_and_names_every_entry(request, tmp_path):
    """Not deleting must not become a silent weakening: the guard still fails, and
    the message still names each entry so a genuinely leaking test is actionable."""

    def leaking_test_body() -> None:
        (tmp_path / "depends_on").mkdir()  # the classic relative-path leak shape
        (tmp_path / "stray_report.json").write_text("{}")

    message = _drive_leak_guard(request, tmp_path, leaking_test_body)

    assert "depends_on" in message
    assert "stray_report.json" in message
    assert "tmp_path" in message  # still tells an actually-leaking test how to fix itself


# ── framework/tooling artifacts are not leaks (piecemeal-mycologic-duckling) ──
#
# The guard diffs REPO_ROOT top-level entries around each test. On the floor
# `sweep (declared lower-bound resolution)` leg a `.pytest_cache` directory
# materializes under REPO_ROOT during a test's wall-clock window (the floor pytest
# writes it despite `-p no:cacheprovider`), so an un-exempted guard fails several
# tests at teardown with `['.pytest_cache']`. Like coverage.py's own data files it
# is gitignored and produced by the framework, not the test body — so it must be
# exempt, while genuine pollution appearing in the same window still fails.


def _run_leak_guard(request: pytest.FixtureRequest, root: Path, during: Callable[[], None]):
    """Drive the REAL ``_no_repo_root_leaks`` teardown against *root*.

    Same hand-driven shape as ``_drive_leak_guard`` but does not presume the
    outcome: returns the guard's failure message when it fails, or ``None`` when it
    passes (no un-exempted entry appeared). This lets a test assert the *passing*
    branch — that an exempt artifact does not trip the guard.
    """
    conftest = _root_conftest(request)
    guard = getattr(conftest._no_repo_root_leaks, "__wrapped__", conftest._no_repo_root_leaks)
    with patch.object(conftest, "_REPO_ROOT", root):
        generator = guard()
        next(generator)
        during()
        try:
            next(generator)
        except StopIteration:
            return None
        except pytest.fail.Exception as failure:
            return str(failure)
    raise AssertionError("guard teardown neither passed nor failed")


def test_leak_guard_exempts_pytest_cache(request, tmp_path):
    """`.pytest_cache` is a pytest-framework artifact — gitignored and written by
    pytest itself, never authored by a test body — so its appearance under
    REPO_ROOT is not a test leak. The guard must pass, exactly as it does for
    coverage.py's data files; otherwise the floor sweep leg fails at teardown."""

    def pytest_writes_its_cache() -> None:
        cache = tmp_path / ".pytest_cache"
        cache.mkdir()
        (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        (cache / "v").mkdir()

    assert _run_leak_guard(request, tmp_path, pytest_writes_its_cache) is None


def test_leak_guard_exempts_every_tooling_cache_artifact(request, tmp_path):
    """Every tool-cache artifact the guard is meant to exempt must actually be
    exempt — not just the reported `.pytest_cache`. The expected names are pinned
    HERE (independent of the implementation set) so the check has teeth: dropping a
    name from the conftest exemption makes that entry reappear un-exempted and turns
    this test RED. Each is a gitignored cache dir written by the toolchain, never by
    a test body."""
    expected_exempt = (
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".mutmut-cache",
        "__pycache__",
    )

    def toolchain_writes_its_caches() -> None:
        for name in expected_exempt:
            (tmp_path / name).mkdir()

    assert _run_leak_guard(request, tmp_path, toolchain_writes_its_caches) is None


def test_leak_guard_still_fails_on_real_pollution_beside_pytest_cache(request, tmp_path):
    """Exempting the framework artifact must not blind the guard: a genuine stray
    entry appearing in the same window still fails the guard and is still named,
    while the exempt `.pytest_cache` is not reported."""

    def cache_plus_a_real_leak() -> None:
        (tmp_path / ".pytest_cache").mkdir()
        (tmp_path / "stray_report.json").write_text("{}")

    message = _run_leak_guard(request, tmp_path, cache_plus_a_real_leak)

    assert message is not None, "a real leak beside .pytest_cache must still fail the guard"
    assert "stray_report.json" in message
    assert ".pytest_cache" not in message


def _drive_working_tree_backstop(
    request: pytest.FixtureRequest, root: Path, during: Callable[[], None]
) -> tuple[str, SimpleNamespace]:
    """Run the REAL session backstop against *root*, returning its text and session.

    Same hand-driven shape as the two guard drivers above: ``pytest_sessionstart``
    takes the porcelain snapshot, *during* stands in for whatever dirtied the tree
    while the session ran, and ``pytest_sessionfinish`` is the hook under test. The
    module-level snapshot is patched so restoring it cannot disturb the real run's
    own backstop.
    """
    conftest = _root_conftest(request)
    written: list[str] = []
    reporter = SimpleNamespace(write_line=lambda line, **kwargs: written.append(line))
    session = SimpleNamespace(
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter),
        ),
        exitstatus=0,
    )
    with (
        patch.object(conftest, "_REPO_ROOT", root),
        patch.object(conftest, "_PORCELAIN_AT_START", None),
    ):
        conftest.pytest_sessionstart(session)
        during()
        conftest.pytest_sessionfinish(session, 0)
    return "\n".join(written), session


def test_session_backstop_still_fails_the_run_and_names_every_entry(request, tmp_path):
    """Rewording must not become a silent weakening: the backstop still escalates the
    run to a failure so CI catches a genuine leak, and still names each entry."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    def a_write_lands_in_the_tree() -> None:
        (repo / "stray_report.json").write_text("{}")

    message, session = _drive_working_tree_backstop(request, repo, a_write_lands_in_the_tree)

    assert "REPO ISOLATION FAILURE" in message
    assert "stray_report.json" in message
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_session_backstop_does_not_assert_a_test_made_the_change(request, tmp_path):
    """Two porcelain snapshots taken around the whole session carry no writer
    identity, so "a test wrote into the working tree" is a claim the backstop cannot
    make — a concurrent write from another process in this checkout is the same diff
    entry. Sibling finding of bugs `746c-185a` and `hot-guessable-ungulate`."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    def another_process_writes() -> None:
        (repo / "EXTERNAL_MUTATION_PROBE").write_text("written by another process")

    message, _ = _drive_working_tree_backstop(request, repo, another_process_writes)

    # It still tells an actually-leaking test how to fix itself.
    assert "tmp_path" in message, message
    assert "a test wrote into the working tree instead of tmp_path" not in message, message
    assert "offending" not in message.lower(), message
    lowered = message.lower()
    assert "concurrent" in lowered, message
    assert "outside" in lowered or "external" in lowered, message
