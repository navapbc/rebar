"""Happy-path contract for the shared jscpd runner (ticket 3ba0)."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _runner_subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers._jscpd")
    except ModuleNotFoundError:
        pytest.fail("the shared jscpd runner is not implemented")


def test_parse_statistics_total(tmp_path: Path) -> None:
    subject = _runner_subject()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        output_dir = Path(command[command.index("--output") + 1])
        (output_dir / "jscpd-report.json").write_text(
            json.dumps(
                {
                    "statistics": {
                        "total": {
                            "clones": 3,
                            "percentage": 12.5,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = subject.run_jscpd(repo_root, run=fake_run)

    assert commands == [
        [
            "jscpd",
            "--reporters",
            "json",
            "--output",
            commands[0][4],
            str(repo_root),
        ]
    ]
    assert result == {"clones": 3, "percentage": 12.5}
