"""Held-out fail-closed oracle for the code-health analyzer seam (ticket 3b30)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar.metrics.analyzer as analyzer_module
from rebar.metrics.analyzer import analyze_or_unavailable, resolve_analyzer
from rebar.metrics.registry import Unavailable

pytestmark = pytest.mark.unit


def test_unconfigured_language_returns_honest_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(analyzer_module, "ANALYZERS", {})
    accruing_since = "2026-02-03T04:05:06+00:00"

    assert resolve_analyzer("go") is None

    result = analyze_or_unavailable(
        "go",
        tmp_path,
        languages=("go",),
        accruing_since=accruing_since,
    )

    assert result == Unavailable(
        reason="no analyzer configured for go",
        accruing_since=accruing_since,
    )


def test_analyzer_import_is_additive_and_unsupported_signals_default_to_none() -> None:
    result = analyzer_module.AnalyzerResult(loc={"files": {}, "max_loc": 0})
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from rebar.metrics.registry import REGISTRY\n"
                "before = tuple(spec.id for spec in REGISTRY)\n"
                "import rebar.metrics.analyzer\n"
                "after = tuple(spec.id for spec in REGISTRY)\n"
                "raise SystemExit(0 if after == before else 1)\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert result.loc == {"files": {}, "max_loc": 0}
    assert result.complexity is None
    assert result.duplication is None
