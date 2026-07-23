"""Held-out fail-closed and subprocess oracle for the scc LOC analyzer."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

pytestmark = pytest.mark.unit

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def _subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.scc_loc")
    except ModuleNotFoundError:
        pytest.fail("the scc LOC analyzer is not implemented")


def test_missing_binary(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def missing(_command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("scc")

    result = _subject().analyze(tmp_path, run=missing)

    assert isinstance(result, Unavailable)
    assert result.accruing_since == _ACCRUING_SINCE
    assert "scc" in result.reason
    assert "scc unavailable:" in caplog.text


@pytest.mark.parametrize(
    "runner",
    [
        lambda command, **_: subprocess.CompletedProcess(command, 2, "", "failed"),
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
        lambda command, **_: subprocess.CompletedProcess(command, 0, "{", ""),
    ],
    ids=["nonzero", "empty", "invalid-json"],
)
def test_bad_output(
    tmp_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    result = _subject().analyze(tmp_path, run=runner)

    assert isinstance(result, Unavailable)
    assert result.reason
    assert result.accruing_since == _ACCRUING_SINCE


def test_empty_tree(tmp_path: Path) -> None:
    def empty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "[]", "")

    result = _subject().analyze(tmp_path, run=empty)

    assert isinstance(result, AnalyzerResult)
    assert result.loc == {"files": {}, "max_loc": 0}


def test_scan_roots_accept_config_strings_in_stable_order(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def empty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    result = _subject().analyze(
        tmp_path,
        scan_roots=["web", "src", "src"],
        run=empty,
    )

    assert isinstance(result, AnalyzerResult)
    assert commands == [
        ["scc", "--format", "json", str((tmp_path / "src").resolve())],
        ["scc", "--format", "json", str((tmp_path / "web").resolve())],
    ]


def test_empty_config_scan_roots_defaults_to_repo_root(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def empty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    result = _subject().analyze(tmp_path, scan_roots=[], run=empty)

    assert isinstance(result, AnalyzerResult)
    assert commands == [["scc", "--format", "json", str(tmp_path.resolve())]]


def test_default_runner_executes_scc_and_normalizes_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('ok')\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_scc = bin_dir / "scc"
    payload = [{"Name": "Python", "Files": [{"Location": str(source), "Code": 1}]}]
    fake_scc.write_text(
        "#!/bin/sh\nprintf '%s\\n' " + repr(json.dumps(payload)) + "\n",
        encoding="utf-8",
    )
    fake_scc.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    result = _subject().analyze(repo_root)

    assert isinstance(result, AnalyzerResult)
    assert result.loc == {"files": {"src/main.py": 1}, "max_loc": 1}


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_live(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("print('ok')\n", encoding="utf-8")

    result = _subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult)
    assert result.loc["files"]["sample.py"] == 1
