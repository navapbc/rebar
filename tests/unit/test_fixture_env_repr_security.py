"""Regression oracle for ambient-secret exposure through pytest fixture reprs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest

pytestmark = pytest.mark.unit

_SECRET_NAME = "REBAR_FIXTURE_SECRET_SENTINEL"
_SECRET_VALUE = "fixture-secret-must-not-appear-in-pytest-output"
_FIXTURE_SURFACES = (
    ("tests.scripts.test_autodeploy_materialize_probe", "deploy_box", "env"),
    ("tests.scripts.test_autodeploy_fetch_secrets", "deploy_box", "env"),
    ("tests.scripts.test_autodeploy_disk_pressure", "box", "env"),
    ("tests.scripts.test_autodeploy_health_gate", "deploy_box", "env"),
    ("tests.scripts.test_autodeploy_signals_gerrit_config", "deploy_box", "env"),
    ("tests.scripts.test_observability_exit", "healthy_env", None),
    ("tests.interfaces.facades.test_cli", "offline_acli_env", None),
)


def _run_fixture_failure(
    tmp_path: Path,
    module: str,
    fixture: str,
    env_key: str | None,
    traceback_style: str,
) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / "test_render_fixture.py"
    value_expr = fixture if env_key is None else f"{fixture}[{env_key!r}]"
    test_file.write_text(
        f"""\
import os
import pytest

pytest_plugins = ({module!r},)

def test_render_fixture({fixture}):
    value = {value_expr}
    assert value[{_SECRET_NAME!r}] == os.environ[{_SECRET_NAME!r}]
    pytest.fail("render the real fixture argument")
"""
    )
    # Put the sentinel first so pytest's bounded mapping repr cannot truncate it.
    child_env = {
        _SECRET_NAME: _SECRET_VALUE,
        "PATH": os.environ.get("PATH", os.defpath),
    }
    arguments = [f"--tb={traceback_style}", "-q", str(test_file)]
    if traceback_style == "long":
        arguments.insert(-2, "--showlocals")
    return run_nested_pytest(
        tmp_path / traceback_style,
        *arguments,
        env=child_env,
        cwd=Path(__file__).parents[2],
    )


@pytest.mark.parametrize(("module", "fixture", "env_key"), _FIXTURE_SURFACES)
def test_fixture_failure_does_not_render_unrelated_ambient_secret(
    tmp_path: Path, module: str, fixture: str, env_key: str | None
) -> None:
    result = _run_fixture_failure(tmp_path, module, fixture, env_key, "long")

    assert result.returncode == 1, result.stdout + result.stderr
    assert _SECRET_VALUE not in result.stdout + result.stderr


@pytest.mark.parametrize(("module", "fixture", "env_key"), _FIXTURE_SURFACES)
def test_short_traceback_is_a_non_rendering_control(
    tmp_path: Path, module: str, fixture: str, env_key: str | None
) -> None:
    result = _run_fixture_failure(tmp_path, module, fixture, env_key, "short")

    assert result.returncode == 1, result.stdout + result.stderr
    assert _SECRET_VALUE not in result.stdout + result.stderr
