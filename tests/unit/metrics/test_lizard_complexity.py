"""Happy-path contract for the lizard complexity analyzer (ticket 21fb)."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from rebar.metrics.analyzer import AnalyzerResult

pytestmark = pytest.mark.unit


def _subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.lizard_complexity")
    except ModuleNotFoundError:
        pytest.fail("the lizard complexity analyzer is not implemented")


def test_analyze_reports_deterministic_per_function_and_aggregate_ccn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subject = _subject()
    analyzed_paths: list[list[str]] = []

    class FakeLizard:
        @staticmethod
        def analyze(paths: list[str]) -> list[SimpleNamespace]:
            analyzed_paths.append(paths)
            return [
                SimpleNamespace(
                    filename=str(repo_root / "web" / "choose.js"),
                    function_list=[
                        SimpleNamespace(
                            name="choose",
                            cyclomatic_complexity=2,
                            start_line=1,
                        )
                    ],
                ),
                SimpleNamespace(
                    filename=str(repo_root / "src" / "choose.py"),
                    function_list=[
                        SimpleNamespace(
                            name="constant",
                            cyclomatic_complexity=1,
                            start_line=7,
                        ),
                        SimpleNamespace(
                            name="choose",
                            cyclomatic_complexity=2,
                            start_line=1,
                        ),
                    ],
                ),
            ]

    monkeypatch.setattr(subject, "guard_import", lambda *_args, **_kwargs: FakeLizard)

    result = subject.analyze(repo_root)

    assert analyzed_paths == [[str(repo_root.resolve())]]
    assert isinstance(result, AnalyzerResult)
    assert result.complexity == {
        "files": {
            "src/choose.py": {
                "functions": [
                    {"name": "choose", "ccn": 2, "start_line": 1},
                    {"name": "constant", "ccn": 1, "start_line": 7},
                ],
                "total_ccn": 3,
                "max_ccn": 2,
            },
            "web/choose.js": {
                "functions": [
                    {"name": "choose", "ccn": 2, "start_line": 1},
                ],
                "total_ccn": 2,
                "max_ccn": 2,
            },
        },
        "functions": 3,
        "total_ccn": 5,
        "max_ccn": 2,
    }
    assert result.loc is None
    assert result.duplication is None
