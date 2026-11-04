"""Tests start the CLI under test, never whichever ``rebar`` comes first on ``PATH``.

A child started as the bare command ``rebar`` (or through ``shutil.which("rebar")``) runs
the first console script on ``PATH``. On a host where an older global install precedes the
worktree venv, that is a different build: ``test_changed_criteria.py`` failed 11 of 18 tests
with a traceback from the global copy (bug ``unglacial-shoddy-rail``), and tests that accept
a non-zero exit passed without ever running the build. ``[sys.executable, "-m", "rebar",
...]`` always runs the interpreter's own ``rebar``.

The scan flags only real child-process launches, so a fake runner or an assertion that
compares an argv list to ``["rebar", ...]`` is not a finding.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHERS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_rebar_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "rebar"


def _launches_bare_rebar(call: ast.Call) -> bool:
    if _callee_name(call) not in _LAUNCHERS or not call.args:
        return False
    argv = call.args[0]
    return (
        isinstance(argv, (ast.List, ast.Tuple))
        and bool(argv.elts)
        and (_is_rebar_literal(argv.elts[0]))
    )


def _looks_up_rebar_on_path(call: ast.Call) -> bool:
    return _callee_name(call) == "which" and bool(call.args) and _is_rebar_literal(call.args[0])


def path_dependent_rebar_calls(tree: ast.AST) -> list[int]:
    """Line numbers of calls that start, or locate, ``rebar`` through ``PATH``."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (_launches_bare_rebar(node) or _looks_up_rebar_on_path(node))
    )


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["rebar", "init"], check=True)',
        'subprocess.Popen(("rebar", "show", tid))',
        'subprocess.check_output(["rebar", *args])',
        'run(["rebar", "list"])',
        'exe = shutil.which("rebar")',
    ],
)
def test_detector_flags_path_dependent_rebar(source: str) -> None:
    assert path_dependent_rebar_calls(ast.parse(source)) == [1]


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run([sys.executable, "-m", "rebar", "init"], check=True)',
        'assert argv[:2] == ["rebar", "list"]',
        'runner.calls.append(["rebar", "transition"])',
        'subprocess.run(["git", "rebar"])',
        'shutil.which("git")',
    ],
)
def test_detector_ignores_build_relative_and_non_launch_uses(source: str) -> None:
    assert path_dependent_rebar_calls(ast.parse(source)) == []


@pytest.mark.repo_policy
def test_no_test_starts_rebar_through_path() -> None:
    modules = parsed_python_files(_TESTS_ROOT)
    assert modules, f"no Python files parsed under {_TESTS_ROOT}"
    offenders = [
        f"{module.relative}:{line}"
        for module in modules
        for line in path_dependent_rebar_calls(module.tree)
    ]
    assert not offenders, (
        "start the CLI under test as [sys.executable, '-m', 'rebar', ...], not through PATH:\n"
        + "\n".join(offenders)
    )
