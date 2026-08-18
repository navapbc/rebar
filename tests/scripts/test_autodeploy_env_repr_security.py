"""Failure rendering must not expose ambient values carried by script fixtures."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "tests" / "scripts" / "test_autodeploy_review_drain.py"


def test_failure_rendering_does_not_expose_ambient_environment(tmp_path: Path) -> None:
    sentinel = "synthetic-not-a-secret-fc43"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text("#!/bin/sh\nexit 91\n")
    fake_bash.chmod(0o755)
    env = {
        "REBAR_DEBUG_SENTINEL": sentinel,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "nested-pytest"),
            f"{_TARGET}::test_a_deploy_that_would_interrupt_a_review_is_deferred",
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, "the nested probe must reach the seeded PATH failure"
    assert "test_a_deploy_that_would_interrupt_a_review_is_deferred" in output
    assert sentinel not in output
