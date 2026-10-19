"""Reject tests.-rooted absolute imports under the bare pytest contract (a371).

Without tests/__init__.py, those namespace imports need the repo root on
sys.path. python -m pytest adds it and can false-green. Bare pytest, used by
make test and CI, adds only tests/unit while conftest adds tests/. An
importorskip above the bad import can mask it further, so this standing
guard scans test modules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"


@pytest.mark.parametrize(
    "module",
    ["test_audit_ticket_page.py", "test_audit_ticket_page_heldout.py"],
)
def test_audit_page_modules_collect_under_bare_pytest(module: str, tmp_path: Path) -> None:
    """The audit-page modules collect under the BARE ``pytest`` console script (bug a371).

    Run from a cwd outside the repository so the repo root cannot reach ``sys.path`` by
    accident -- the exact condition under which the reported ``ModuleNotFoundError: No
    module named 'tests'`` fired.
    """
    pytest.importorskip("fastapi")  # the [ui] extra; absent from the lean CI suite
    pytest.importorskip("httpx")  # starlette TestClient's HTTP backend

    console_script = Path(sys.executable).parent / "pytest"
    if not console_script.exists():  # pragma: no cover - environment without the script
        pytest.skip("no `pytest` console script next to the running interpreter")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [
            str(console_script),
            str(_TESTS_DIR / "unit" / module),
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            # A child without this allocates into the SHARED numbered temp root and can
            # delete another session's root as pytest prunes it (bug 16e1-237d).
            "--basetemp",
            str(tmp_path / "collect-only-pytest"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (
        f"bare `pytest --collect-only` failed for {module}:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "ModuleNotFoundError" not in proc.stdout + proc.stderr
