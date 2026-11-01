"""Happy-path contract for the shared jscpd runner (ticket 3ba0).

Held-out oracles merged from test_jscpd_dup_heldout: Held-out adapter, reuse, and failure
contracts for jscpd (ticket 3ba0).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

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
    """Resolve ``subprocess.run`` at call time so later patches control the jscpd runner."""
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


# ── held-out oracles (merged from test_jscpd_dup_heldout) ──

_BACKFILL = (
    Path(__file__).resolve().parents[3] / "scripts" / "backfill_complexity_clone_snapshots.py"
)


def _adapter_subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.jscpd_dup")
    except ModuleNotFoundError:
        pytest.fail("the jscpd duplication analyzer is not implemented")


def _load_backfill() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_jscpd_reuse", _BACKFILL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adapter_and_backfill_reuse_one_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared = _runner_subject()
    adapter = _adapter_subject()
    backfill = _load_backfill()
    calls: list[str | Path] = []

    def fake_run_jscpd(scan_root: str | Path) -> dict[str, int | float]:
        calls.append(scan_root)
        return {"clones": 4, "percentage": 8.25}

    assert adapter.run_jscpd is shared.run_jscpd
    assert backfill.run_jscpd is shared.run_jscpd
    monkeypatch.setattr(adapter, "run_jscpd", fake_run_jscpd)
    monkeypatch.setattr(backfill, "run_jscpd", fake_run_jscpd)
    monkeypatch.setattr(backfill, "_measure_complexity", lambda _tree: 11)

    analyzed = adapter.analyze(tmp_path)
    measured = backfill.measure_tree(tmp_path)

    assert isinstance(analyzed, AnalyzerResult)
    assert analyzed.duplication == {"clones": 4, "percentage": 8.25}
    assert analyzed.loc is None
    assert analyzed.complexity is None
    assert measured == {"complexity": 11, "clone_count": 4}
    assert calls == [tmp_path.resolve(), str(tmp_path)]


def test_absent_jscpd_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = _adapter_subject()

    def missing(_scan_root: str | Path) -> None:
        raise FileNotFoundError("jscpd executable not found")

    monkeypatch.setattr(adapter, "run_jscpd", missing)

    result = adapter.analyze(tmp_path)

    assert isinstance(result, Unavailable)
    assert "jscpd" in result.reason
    assert "not found" in result.reason
    assert result.accruing_since == "2026-01-01T00:00:00+00:00"


def test_failed_jscpd_is_unavailable_not_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared = _runner_subject()
    adapter = _adapter_subject()

    def run_nonzero(scan_root: str | Path) -> dict[str, int | float]:
        def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 2, "", "failed")

        return shared.run_jscpd(scan_root, run=fail)

    monkeypatch.setattr(adapter, "run_jscpd", run_nonzero)

    result = adapter.analyze(tmp_path)

    assert isinstance(result, Unavailable)
    assert "jscpd" in result.reason


def _js_source(prefix: str, statements: int) -> str:
    """Build duplicated JavaScript well above jscpd's default 50-token floor."""

    body = "".join(
        f"  const {prefix}_{index} = ({prefix}_seed + {index}) * {index + 2};\n"
        for index in range(statements)
    )
    return f"function {prefix}_compute({prefix}_seed) {{\n{body}  return {prefix}_0;\n}}\n"


def test_zero_scanned_sources_is_unavailable_not_a_confident_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A jscpd report with zero scanned sources yields unavailable instead of zero."""

    shared = _runner_subject()
    adapter = _adapter_subject()

    def run_empty(scan_root: str | Path) -> dict[str, int | float]:
        def no_sources(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            output_dir = Path(command[command.index("--output") + 1])
            (output_dir / "jscpd-report.json").write_text(
                json.dumps(
                    {
                        "statistics": {
                            "total": {
                                "clones": 0,
                                "percentage": 0.0,
                                "sources": 0,
                                "lines": 0,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        return shared.run_jscpd(scan_root, run=no_sources)

    monkeypatch.setattr(adapter, "run_jscpd", run_empty)

    result = adapter.analyze(tmp_path)

    assert isinstance(result, Unavailable)
    assert "jscpd" in result.reason
    assert result.accruing_since == "2026-01-01T00:00:00+00:00"


@pytest.mark.integration
def test_live_clone(tmp_path: Path) -> None:
    if shutil.which("jscpd") is None:
        pytest.skip("jscpd is not installed")

    clone = _js_source("alpha", 20)
    (tmp_path / "first.js").write_text(clone, encoding="utf-8")
    (tmp_path / "second.js").write_text(clone, encoding="utf-8")

    result = _adapter_subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult)
    assert result.duplication["clones"] >= 1
    assert result.duplication["percentage"] > 0


@pytest.mark.integration
def test_live_distinct_sources_report_no_clone(tmp_path: Path) -> None:
    """Negative control: detection must discriminate, not report clones for anything.

    Without this, a detector that reported a clone unconditionally would still
    satisfy ``test_live_clone``. Both files clear the token floor, so a zero
    here means "no duplication found", not "nothing was examined".
    """

    if shutil.which("jscpd") is None:
        pytest.skip("jscpd is not installed")

    (tmp_path / "first.js").write_text(_js_source("alpha", 20), encoding="utf-8")
    (tmp_path / "second.js").write_text(_js_source("bravo", 24), encoding="utf-8")

    result = _adapter_subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult)
    assert result.duplication["clones"] == 0
    assert result.duplication["percentage"] == 0
