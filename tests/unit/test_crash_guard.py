from __future__ import annotations

import ast
import os
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
