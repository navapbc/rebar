"""Held-out adapter, reuse, and failure contracts for jscpd (ticket 3ba0)."""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

pytestmark = pytest.mark.unit

_BACKFILL = (
    Path(__file__).resolve().parents[3] / "scripts" / "backfill_complexity_clone_snapshots.py"
)


def _adapter_subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.jscpd_dup")
    except ModuleNotFoundError:
        pytest.fail("the jscpd duplication analyzer is not implemented")


def _runner_subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers._jscpd")
    except ModuleNotFoundError:
        pytest.fail("the shared jscpd runner is not implemented")


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


@pytest.mark.integration
def test_live_clone(tmp_path: Path) -> None:
    if shutil.which("jscpd") is None:
        pytest.skip("jscpd is not installed")

    first = tmp_path / "first.js"
    second = tmp_path / "second.js"
    clone = (
        "function choose(value) {\n"
        "  const normalized = value + 1;\n"
        "  if (normalized > 5) {\n"
        "    return normalized * 2;\n"
        "  }\n"
        "  return normalized - 2;\n"
        "}\n"
    )
    first.write_text(clone, encoding="utf-8")
    second.write_text(clone, encoding="utf-8")

    result = _adapter_subject().analyze(tmp_path)

    assert isinstance(result, AnalyzerResult)
    assert result.duplication["clones"] >= 1
    assert result.duplication["percentage"] > 0
