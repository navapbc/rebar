"""Held-out adversarial checks for the subprocess environment policy guard."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORACLE_PATH = _REPO_ROOT / "tests" / "unit" / "test_subprocess_env_repr_security.py"
_BOUNDARY_PATH = _REPO_ROOT / "tests" / "_subprocess_env.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unclassified_fixture_raw_environment_is_detected() -> None:
    oracle = _load(_ORACLE_PATH, "subprocess_env_oracle_fixture")
    tree = ast.parse(
        """
import os
import pytest

@pytest.fixture
def newly_added_fixture():
    return dict(os.environ)
"""
    )

    assert len(oracle._raw_env_nodes(tree)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "base: object = subprocess_env()\nenv = dict(base)",
        "base = subprocess_env().copy()\nenv = dict(base)",
        ("from _subprocess_env import subprocess_env as safe_env\nenv = dict(safe_env())"),
    ],
)
def test_boundary_unwrap_alias_bypasses_are_detected(source: str) -> None:
    oracle = _load(_ORACLE_PATH, "subprocess_env_oracle_alias")

    assert len(oracle._boundary_unwrap_nodes(source)) == 1


def test_successful_child_receives_inherited_and_overridden_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _load(_BOUNDARY_PATH, "subprocess_env_boundary")
    monkeypatch.setenv("REBAR_HELDOUT_INHERITED", "inherited-value")
    env = boundary.subprocess_env(REBAR_HELDOUT_OVERRIDE="override-value")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, os; "
                "print(json.dumps([os.environ['REBAR_HELDOUT_INHERITED'], "
                "os.environ['REBAR_HELDOUT_OVERRIDE']]))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == ["inherited-value", "override-value"]
