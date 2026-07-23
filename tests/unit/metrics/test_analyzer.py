"""Happy-path contract for the code-health analyzer seam (ticket 3b30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.metrics.analyzer import (
    ANALYZERS,
    AnalyzerResult,
    analyze_or_unavailable,
    resolve_analyzer,
)

pytestmark = pytest.mark.unit


class _RecordingAnalyzer:
    def __init__(self, result: AnalyzerResult) -> None:
        self.result = result
        self.calls: list[tuple[Path, tuple[str, ...] | None]] = []

    def analyze(
        self,
        repo_root: Path,
        languages: tuple[str, ...] | None = None,
    ) -> AnalyzerResult:
        self.calls.append((repo_root, languages))
        return self.result


def test_configured_analyzer_is_resolved_and_result_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = AnalyzerResult(
        loc={"files": {"src/main.py": 12}, "max_loc": 12},
        complexity={"functions": 1, "max_ccn": 2},
        duplication=None,
    )
    configured = _RecordingAnalyzer(expected)
    monkeypatch.setitem(ANALYZERS, "python", configured)

    assert resolve_analyzer("python") is configured

    result = analyze_or_unavailable(
        "python",
        tmp_path,
        languages=("python",),
        accruing_since="2026-01-01T00:00:00+00:00",
    )

    assert result is expected
    assert configured.calls == [(tmp_path, ("python",))]
