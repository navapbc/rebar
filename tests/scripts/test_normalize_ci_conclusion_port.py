"""Port oracle for ticket 9d07: the normalize_ci_conclusion shell script -> Python (happy paths).

The REAL contract is two-input: f(CONCLUSION, FAILURE_OBSERVED) -> vote in
{success, failure, cancelled}, never out-of-domain — consumed by
.github/workflows/gerrit-verify.yaml's Verified-vote job. The port must be a
stdlib-only single file (the workflow sparse-checks-out exactly that one path and
runs it without the package installed), expose `normalize(conclusion,
failure_observed) -> str` for in-process tests, and keep the env-driven CLI shape
(CONCLUSION / FAILURE_OBSERVED env vars, one vote on stdout).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_REPO = Path(__file__).resolve().parents[2]
_PY = _REPO / "scripts" / "normalize_ci_conclusion.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "gerrit-verify.yaml"

_VALID = {"success", "failure", "cancelled"}


def _mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("normalize_ci_conclusion", _PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_entry_point_exists() -> None:
    assert _PY.exists(), f"missing ported entry point: {_PY}"


@pytest.mark.parametrize(
    ("conclusion", "failure_observed", "expected"),
    [
        # Pass-through rows: FAILURE_OBSERVED is irrelevant — assert BOTH values.
        ("success", "false", "success"),
        ("success", "true", "success"),
        ("failure", "false", "failure"),
        ("failure", "true", "failure"),
        ("cancelled", "false", "cancelled"),
        ("cancelled", "true", "cancelled"),
        # im-open's `skipped` fallback: benign unless a needed job really failed.
        ("skipped", "false", "success"),
        ("skipped", "true", "failure"),
        # Fail-closed anomaly rows.
        ("", "false", "failure"),
        ("timed_out", "false", "failure"),
    ],
)
def test_function_mapping(conclusion: str, failure_observed: str, expected: str) -> None:
    got = _mod().normalize(conclusion, failure_observed)
    assert got == expected
    assert got in _VALID


def test_cli_env_contract_matches_shell_shape() -> None:
    """Env-driven subprocess parity: CONCLUSION/FAILURE_OBSERVED in, one vote on stdout."""
    cp = subprocess.run(
        [sys.executable, str(_PY)],
        env={"CONCLUSION": "skipped", "FAILURE_OBSERVED": "false", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "success"


def test_workflow_invokes_python_port() -> None:
    """gerrit-verify.yaml sparse-checks-out the .py path and the run step invokes it."""
    text = _WORKFLOW.read_text()
    assert "scripts/normalize_ci_conclusion.py" in text
    assert ("normalize_ci_conclusion" + ".sh") not in text  # split: avoid self-matching scans
