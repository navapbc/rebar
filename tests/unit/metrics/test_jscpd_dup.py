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


def _write_minimal_report(command: list[str], clones: int = 1, percentage: float = 2.0) -> None:
    output_dir = Path(command[command.index("--output") + 1])
    (output_dir / "jscpd-report.json").write_text(
        json.dumps({"statistics": {"total": {"clones": clones, "percentage": percentage}}}),
        encoding="utf-8",
    )


def test_default_run_resolves_subprocess_run_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``subprocess.run`` patch applied AFTER the module is imported must reach
    ``run_jscpd``'s default runner (bug 9118, same frozen-default class as 2c4b/5ea3).

    The hostile order: the module is already resident in ``sys.modules``, THEN the
    test patches ``subprocess.run`` on its defining module. A frozen
    ``run: Runner = subprocess.run`` default captured the original function at
    import and silently escapes the patch (invoking the REAL external jscpd).
    """
    subject = _runner_subject()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        _write_minimal_report(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = subject.run_jscpd(tmp_path)  # default run

    assert len(calls) == 1, "the subprocess.run patch did not reach the default runner"
    assert result == {"clones": 1, "percentage": 2.0}


def test_explicit_run_wins_over_the_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Call-time resolution must not override an explicit injection."""
    subject = _runner_subject()

    def fail_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        pytest.fail("the seam must not be consulted when run= is passed explicitly")

    monkeypatch.setattr(subprocess, "run", fail_run)
    injected: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        injected.append(command)
        _write_minimal_report(command, clones=4, percentage=9.0)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = subject.run_jscpd(tmp_path, run=fake_run)

    assert len(injected) == 1
    assert result == {"clones": 4, "percentage": 9.0}
