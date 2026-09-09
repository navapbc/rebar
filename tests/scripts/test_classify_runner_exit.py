from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "classify_runner_exit.py"


def _run(status: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(status)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_zero_status_is_success() -> None:
    completed = _run(0)
    assert completed.returncode == 0
    assert "success" in completed.stdout.lower()


def test_names_sigkill_status_137_as_likely_oom() -> None:
    completed = _run(137)
    assert completed.returncode == 1
    assert "SIGKILL" in completed.stdout
    assert "OOM" in completed.stdout


def test_names_sigsegv_status_139() -> None:
    completed = _run(139)
    assert completed.returncode == 1
    assert "SIGSEGV" in completed.stdout


def test_names_timeout_status_124() -> None:
    completed = _run(124)
    assert completed.returncode == 1
    assert "timeout" in completed.stdout.lower()
