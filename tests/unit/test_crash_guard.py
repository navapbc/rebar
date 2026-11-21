from __future__ import annotations

import ast
import os
import re
import textwrap
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env


def _env_with_tests_on_path() -> dict[str, str]:
    env = subprocess_env()
    tests_dir = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = tests_dir if not existing else os.pathsep.join([tests_dir, existing])
    return env


def _crashing_xdist_run(tmp_path: Path):
    project = tmp_path / "crash_probe"
    project.mkdir()
    (project / "test_worker_crash.py").write_text(
        textwrap.dedent(
            """\
            import os


            def test_worker_dies():
                os._exit(1)


            def test_other_test_passes():
                assert True
            """
        ),
        encoding="utf-8",
    )
    return run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "-n",
        "2",
        "-p",
        "_crash_guard",
        "test_worker_crash.py",
        env=_env_with_tests_on_path(),
        cwd=project,
        timeout=120,
    )


@pytest.mark.timeout(180)
def test_crashed_xdist_worker_names_the_dead_worker(tmp_path: Path) -> None:
    completed = _crashing_xdist_run(tmp_path)
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert "rebar crash guard" in combined
    assert "worker=gw" in combined
    assert "node down" in combined.lower() or "not properly terminated" in combined.lower()


@pytest.mark.timeout(180)
def test_crashed_xdist_worker_is_not_masked_by_passing_tests(tmp_path: Path) -> None:
    completed = _crashing_xdist_run(tmp_path)
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert "test_other_test_passes" not in combined or "rebar crash guard" in combined


def test_crash_guard_uses_xdist_hook_not_output_scraping() -> None:
    source = Path(__file__).resolve().parents[1] / "_crash_guard.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)
    function_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "pytest_testnodedown" in function_names
    assert "node down" not in text.lower()
    assert "worker crashed" not in text.lower()


@pytest.mark.timeout(180)
def test_single_process_run_is_untouched(tmp_path: Path) -> None:
    project = tmp_path / "single_process_probe"
    project.mkdir()
    (project / "test_passes.py").write_text(
        "def test_passes():\n    assert True\n", encoding="utf-8"
    )
    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "-p",
        "_crash_guard",
        "test_passes.py",
        env=_env_with_tests_on_path(),
        cwd=project,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "rebar crash guard" not in combined


@pytest.mark.timeout(180)
def test_crash_guard_is_noop_when_xdist_hook_is_absent(tmp_path: Path) -> None:
    project = tmp_path / "no_xdist_probe"
    project.mkdir()
    (project / "test_passes.py").write_text(
        "def test_passes():\n    assert True\n", encoding="utf-8"
    )
    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "-p",
        "no:xdist",
        "-p",
        "_crash_guard",
        "test_passes.py",
        env=_env_with_tests_on_path(),
        cwd=project,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "unknown hook 'pytest_testnodedown'" not in combined


def test_crash_guard_xdist_hook_is_optional_without_xdist() -> None:
    source = Path(__file__).resolve().parents[1] / "_crash_guard.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    hook = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_testnodedown"
    )

    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "hookimpl"
        and any(
            keyword.arg == "optionalhook"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        )
        for decorator in hook.decorator_list
    ), "pytest_testnodedown must be optional when pytest-xdist is absent"


def test_conftest_registers_crash_guard_plugin() -> None:
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    tree = ast.parse(conftest.read_text(encoding="utf-8"))
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
    plugin_values = []
    for assignment in assignments:
        if any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins"
            for target in assignment.targets
        ):
            plugin_values.append(ast.literal_eval(assignment.value))
    assert plugin_values, "tests/conftest.py must register tests/_crash_guard.py as a plugin"
    flattened = {
        item
        for value in plugin_values
        for item in (value if isinstance(value, (list, tuple)) else [value])
    }
    assert "_crash_guard" in flattened


_HANGING_MODULE = """\
import time


def test_hangs_past_its_budget():
    time.sleep(60)
"""
_HANGING_NODE_ID = "test_hangs.py::test_hangs_past_its_budget"


def _timed_out_run(tmp_path: Path, *xdist_args: str):
    """Run one hanging test past a 2 s budget under the repo's thread timeout method.

    The tmp project has no ini, and pytest-timeout's POSIX default is ``signal``, so the
    thread method that pyproject.toml configures is pinned explicitly.
    """
    project = tmp_path / "timeout_probe"
    project.mkdir()
    (project / "test_hangs.py").write_text(_HANGING_MODULE, encoding="utf-8")
    return run_nested_pytest(
        tmp_path / "nested",
        "-q",
        *xdist_args,
        "-p",
        "_crash_guard",
        "-o",
        "timeout=2",
        "-o",
        "timeout_method=thread",
        "test_hangs.py",
        env=_env_with_tests_on_path(),
        cwd=project,
        timeout=120,
    )


@pytest.mark.timeout(180)
def test_xdist_thread_timeout_names_itself_and_the_test(tmp_path: Path) -> None:
    """A thread-method timeout in a worker must not read as an unexplained crash (bug d1de).

    The worker's fd 1 is /dev/null under execnet, so pytest-timeout's own dump is lost; the
    controller output must still say it was a timeout, and in which test.
    """
    completed = _timed_out_run(tmp_path, "-n", "2")
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert any("Timeout" in line and _HANGING_NODE_ID in line for line in combined.splitlines()), (
        combined
    )


@pytest.mark.timeout(180)
def test_single_process_thread_timeout_keeps_pytest_timeouts_own_report(tmp_path: Path) -> None:
    completed = _timed_out_run(tmp_path)
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    # pytest-timeout's own banner (terminal.sep("+", "Timeout")) reaches the real terminal.
    assert re.search(r"\+{3,} Timeout \+{3,}", combined), combined


# execnet's worker bootstrap (``init_popen_io``) applies this only when ``os.name == "nt"``:
# the real stderr survives solely as the ``sys.stderr`` object and fd 2 becomes the null
# device. An execnet worker is ``python -c ...``, so ``sys.argv == ["-c"]`` selects workers.
# On Windows execnet already does it; emulating it there too would stack a second redirect
# (this runs first, so execnet would then dup the NUL fd 2), so it applies off Windows only.
_WINDOWS_WORKER_STDERR = """\
import os
import sys

if os.name != "nt" and sys.argv[:1] == ["-c"]:
    sys.stderr = os.fdopen(os.dup(2), "w", 1)
    _null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null, 2)
    os.close(_null)
"""


@pytest.mark.timeout(180)
def test_xdist_timeout_banner_survives_windows_worker_stderr(tmp_path: Path) -> None:
    """On Windows a worker's fd 2 is NUL; the banner must use ``sys.stderr`` (bug c691-bbc1)."""
    site = tmp_path / "windows_worker_site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_WINDOWS_WORKER_STDERR, encoding="utf-8")
    project = tmp_path / "timeout_probe"
    project.mkdir()
    (project / "test_hangs.py").write_text(_HANGING_MODULE, encoding="utf-8")
    env = _env_with_tests_on_path()
    env["PYTHONPATH"] = os.pathsep.join([str(site), env["PYTHONPATH"]])
    completed = run_nested_pytest(
        tmp_path / "nested",
        "-q",
        "-n",
        "2",
        "-p",
        "_crash_guard",
        "-o",
        "timeout=2",
        "-o",
        "timeout_method=thread",
        "test_hangs.py",
        env=env,
        cwd=project,
        timeout=120,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0, combined
    assert any("Timeout" in line and _HANGING_NODE_ID in line for line in combined.splitlines()), (
        combined
    )
