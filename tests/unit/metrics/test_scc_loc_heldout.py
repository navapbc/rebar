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


@pytest.mark.parametrize(
    "stdout",
    [
        "[]",
        # scc's SUMMARY mode: the per-language `Files` key is present but EMPTY. Every type
        # check in the parser passes, so the adapter silently measures nothing (ticket c5b3).
        json.dumps([{"Name": "Python", "Files": []}, {"Name": "JSON", "Files": []}]),
    ],
    ids=["no-language-groups", "language-groups-with-empty-file-lists"],
)
def test_empty_file_list_is_unavailable_not_a_confident_zero(tmp_path: Path, stdout: str) -> None:
    """An empty measurement must be Unavailable: a zero means "measured zero"."""

    def empty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = _subject().analyze(tmp_path, run=empty)

    assert isinstance(result, Unavailable), (
        "an empty file list must report Unavailable, never a zero-valued AnalyzerResult"
    )
    assert result.reason
    assert result.accruing_since == _ACCRUING_SINCE


def test_scan_roots_accept_config_strings_in_stable_order(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def one_file(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = [{"Name": "Python", "Files": [{"Location": "a.py", "Lines": 1}]}]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = _subject().analyze(
        tmp_path,
        scan_roots=["web", "src", "src"],
        run=one_file,
    )

    assert isinstance(result, AnalyzerResult)
    assert commands == [
        ["scc", "--by-file", "--format", "json", str((tmp_path / "src").resolve())],
        ["scc", "--by-file", "--format", "json", str((tmp_path / "web").resolve())],
    ]


def test_empty_config_scan_roots_defaults_to_repo_root(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def one_file(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = [{"Name": "Python", "Files": [{"Location": "a.py", "Lines": 1}]}]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = _subject().analyze(tmp_path, scan_roots=[], run=one_file)

    assert isinstance(result, AnalyzerResult)
    assert commands == [["scc", "--by-file", "--format", "json", str(tmp_path.resolve())]]


def test_include_extensions_narrow_the_scan_without_hardcoding_a_language(
    tmp_path: Path,
) -> None:
    """The file-type narrowing is per-project configuration, not a baked-in filter."""

    commands: list[list[str]] = []

    def one_file(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = [{"Name": "Python", "Files": [{"Location": "a.py", "Lines": 1}]}]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = _subject().analyze(
        tmp_path,
        scan_roots=[],
        include_extensions=["py", "pyi"],
        run=one_file,
    )

    assert isinstance(result, AnalyzerResult)
    assert commands == [
        [
            "scc",
            "--by-file",
            "--include-ext",
            "py,pyi",
            "--format",
            "json",
            str(tmp_path.resolve()),
        ]
    ]


def test_default_configuration_stays_polyglot(tmp_path: Path) -> None:
    """No extension filter is applied unless the project configures one."""

    commands: list[list[str]] = []

    def one_file(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = [{"Name": "Go", "Files": [{"Location": "a.go", "Lines": 3}]}]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    _subject().analyze(tmp_path, include_extensions=[], run=one_file)

    assert commands and "--include-ext" not in commands[0]


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
    payload = [{"Name": "Python", "Files": [{"Location": str(source), "Lines": 1}]}]
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


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_live_scan_root_with_files_never_reports_a_confident_zero(tmp_path: Path) -> None:
    """The ticket-c5b3 regression: real scc, real files, no fabricated zero."""

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "small.py").write_text("x = 1\n" * 4, encoding="utf-8")
    (package / "large.py").write_text("y = 2\n" * 40, encoding="utf-8")

    result = _subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult), f"expected a measurement, got {result!r}"
    assert result.loc["files"], "a scan root that demonstrably contains files measured nothing"
    assert result.loc["max_loc"] > 0
    assert result.loc["files"]["pkg/large.py"] == 40


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_live_reported_loc_matches_wc_l(tmp_path: Path) -> None:
    """AC4: the metric measures raw line count, exactly what the CI size gate counts."""

    source = tmp_path / "module.py"
    body = "# comment\n\nvalue = 1\n" * 7
    source.write_text(body, encoding="utf-8")
    expected = len(body.splitlines())

    result = _subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult)
    assert result.loc["files"]["module.py"] == expected


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_live_include_extensions_excludes_assets_and_resources(tmp_path: Path) -> None:
    """AC3: a bundled asset and a resource .txt are excluded by project configuration."""

    (tmp_path / "module.py").write_text("value = 1\n" * 5, encoding="utf-8")
    (tmp_path / "wordlist.txt").write_text("word\n" * 900, encoding="utf-8")
    (tmp_path / "bundle.js").write_text("var a = 1;\n" * 700, encoding="utf-8")

    unfiltered = _subject().analyze(tmp_path)
    assert isinstance(unfiltered, AnalyzerResult)
    assert "wordlist.txt" in unfiltered.loc["files"]

    result = _subject().analyze(tmp_path, include_extensions=["py"])

    assert isinstance(result, AnalyzerResult)
    assert set(result.loc["files"]) == {"module.py"}
    assert result.loc["max_loc"] == 5
