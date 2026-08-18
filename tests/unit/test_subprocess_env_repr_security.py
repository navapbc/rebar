"""Regression oracles for test subprocess environment traceback safety."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"
_BOUNDARY_MODULE = _TESTS_ROOT / "_subprocess_env.py"
_SENTINEL_NAME = "REBAR_SUBPROCESS_ENV_TRACE_SENTINEL"
_SENTINEL_VALUE = "synthetic-not-a-secret-5bc2-2a95"


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _raw_env_audit(
    tree: ast.AST, relative_path: str = "<memory>"
) -> tuple[list[ast.AST], set[tuple[str, str]]]:
    del relative_path
    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        is_dict_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and bool(node.args)
            and _is_os_environ(node.args[0])
        )
        is_copy = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "copy"
            and _is_os_environ(node.func.value)
        )
        is_unpack = isinstance(node, ast.Dict) and any(
            key is None and _is_os_environ(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
        if not (is_dict_call or is_copy or is_unpack):
            continue
        candidates.append(node)
    return candidates, set()


def _raw_env_nodes(tree: ast.AST, relative_path: str = "<memory>") -> list[ast.AST]:
    return _raw_env_audit(tree, relative_path)[0]


def _raw_env_audit_sites() -> tuple[list[str], set[tuple[str, str]]]:
    offenders: list[str] = []
    active_exclusions: set[tuple[str, str]] = set()
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path == _BOUNDARY_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_REPO_ROOT)
        hits, excluded = _raw_env_audit(tree, relative.as_posix())
        offenders.extend(f"{relative}:{node.lineno}" for node in hits)
        active_exclusions.update(excluded)
    return offenders, active_exclusions


def _raw_env_sites() -> list[str]:
    return _raw_env_audit_sites()[0]


def _boundary_unwrap_nodes(source: str) -> list[ast.AST]:
    tree = ast.parse(source)
    factory_names = {"subprocess_env"}
    boundary_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in {"tests._subprocess_env", "_subprocess_env"}:
                for imported in node.names:
                    if imported.name == "subprocess_env":
                        factory_names.add(imported.asname or imported.name)
            elif node.module == "tests":
                for imported in node.names:
                    if imported.name == "_subprocess_env":
                        boundary_module_names.add(imported.asname or imported.name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in {"tests._subprocess_env", "_subprocess_env"}:
                    boundary_module_names.add(imported.asname or imported.name.split(".")[0])

    aliases: set[str] = set()

    def assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [target.id for target in targets if isinstance(target, ast.Name)]

    def is_boundary(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in factory_names
        if not isinstance(node.func, ast.Attribute):
            return False
        if (
            node.func.attr == "subprocess_env"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in boundary_module_names
        ):
            return True
        return node.func.attr in {"copy", "with_overrides"} and is_boundary(node.func.value)

    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            if value is not None and is_boundary(value):
                for name in assigned_names(assignment):
                    if name not in aliases:
                        aliases.add(name)
                        changed = True

    hits: list[ast.AST] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and node.args
            and is_boundary(node.args[0])
        ):
            hits.append(node)
        elif isinstance(node, ast.Dict) and any(
            key is None and is_boundary(value)
            for key, value in zip(node.keys, node.values, strict=True)
        ):
            hits.append(node)
    return hits


def _boundary_unwrap_sites() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path == _BOUNDARY_MODULE:
            continue
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO_ROOT)
        offenders.extend(f"{relative}:{node.lineno}" for node in _boundary_unwrap_nodes(source))
    return offenders


def _run_traceback_probe(tmp_path: Path, traceback_style: str) -> subprocess.CompletedProcess[str]:
    target = _TESTS_ROOT / "integration" / "test_init_fail_closed.py"
    missing = tmp_path / "missing-executable"
    probe = tmp_path / "test_subprocess_startup_failure.py"
    probe.write_text(
        f"""\
import importlib.util
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("init_fail_closed_probe", {str(target)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_subprocess_startup_failure():
    env = module._env(Path.cwd())
    assert {_SENTINEL_NAME!r} in env
    subprocess.run([{str(missing)!r}], env=env, check=True)
""",
        encoding="utf-8",
    )
    child_env = {
        _SENTINEL_NAME: _SENTINEL_VALUE,
        "PATH": os.environ.get("PATH", os.defpath),
        # The generated probe lives outside the repository's tests tree, so it does not
        # inherit tests/conftest.py's normal insertion of tests/ for shared helpers.
        "PYTHONPATH": str(_TESTS_ROOT),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / f"pytest-{traceback_style}"),
            f"--tb={traceback_style}",
            str(probe),
        ],
        cwd=_REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_all_non_fixture_subprocess_environments_use_the_safe_boundary() -> None:
    offenders, active_exclusions = _raw_env_audit_sites()

    assert active_exclusions == set()
    assert offenders == []


def test_safe_boundary_is_not_unwrapped_in_repository_tests() -> None:
    assert _boundary_unwrap_sites() == []


def test_new_raw_environment_fixture_is_not_blanket_excluded() -> None:
    tree = ast.parse(
        """\
@pytest.fixture
def newly_added_env():
    return dict(os.environ)
"""
    )

    hits, exclusions = _raw_env_audit(tree, "tests/unit/test_new_surface.py")

    assert len(hits) == 1
    assert exclusions == set()


def test_raw_environment_fixture_reports_every_constructor() -> None:
    tree = ast.parse(
        """\
@pytest.fixture
def offline_acli_env():
    first = dict(os.environ)
    return os.environ.copy()
"""
    )

    hits, exclusions = _raw_env_audit(tree, "tests/interfaces/facades/test_cli.py")

    assert len(hits) == 2
    assert exclusions == set()


@pytest.mark.parametrize("traceback_style", ["long", "short"])
def test_startup_failure_does_not_render_inherited_environment(
    tmp_path: Path, traceback_style: str
) -> None:
    result = _run_traceback_probe(tmp_path, traceback_style)

    assert result.returncode == 1, result.stdout + result.stderr
    assert _SENTINEL_VALUE not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "env = dict(subprocess_env())",
        "base = subprocess_env()\nenv = dict(base)",
        "base: SubprocessEnv = subprocess_env()\nenv = dict(base)",
        "base = subprocess_env()\nalias = base\nenv = dict(alias)",
        "base = subprocess_env()\nalias: SubprocessEnv = base\nenv = dict(alias)",
        "base = subprocess_env()\ncopied = base.copy()\nenv = dict(copied)",
        "env = {**subprocess_env(), 'EXTRA': 'value'}",
        "base = subprocess_env()\nenv = {**base, 'EXTRA': 'value'}",
        "from tests._subprocess_env import subprocess_env as safe_env\nenv = dict(safe_env())",
        "import tests._subprocess_env as envs\nenv = {**envs.subprocess_env()}",
    ],
)
def test_boundary_unwrapping_is_detected(source: str) -> None:
    assert len(_boundary_unwrap_nodes(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "base = subprocess_env()\nenv = base.copy()",
        "base = subprocess_env()\nenv = base.with_overrides(EXTRA='value')",
        "env = subprocess_env().copy().with_overrides(EXTRA='value')",
    ],
)
def test_boundary_preserving_derivations_are_sanctioned(source: str) -> None:
    assert _boundary_unwrap_nodes(source) == []


def test_successful_child_receives_inherited_and_overridden_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited_name = "REBAR_SUBPROCESS_ENV_INHERITED"
    override_name = "REBAR_SUBPROCESS_ENV_OVERRIDE"
    monkeypatch.setenv(inherited_name, "inherited-exact-value")
    monkeypatch.setenv(override_name, "original-value")
    env = subprocess_env({override_name: "overridden-exact-value"})

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"print(os.environ[{inherited_name!r}]); "
                f"print(os.environ[{override_name!r}])"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.splitlines() == ["inherited-exact-value", "overridden-exact-value"]
