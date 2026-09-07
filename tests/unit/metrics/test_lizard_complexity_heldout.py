"""Held-out edge and polyglot contracts for lizard complexity (ticket 21fb)."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from rebar._optional import OptionalDependencyError
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

pytestmark = pytest.mark.unit


def _subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.lizard_complexity")
    except ModuleNotFoundError:
        pytest.fail("the lizard complexity analyzer is not implemented")


def test_real_lizard_analyzes_python_and_javascript(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    source = repo_root / "src"
    source.mkdir(parents=True)
    (source / "choose.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n",
        encoding="utf-8",
    )
    (source / "choose.js").write_text(
        "function choose(value) {\n  if (value) {\n    return 1;\n  }\n  return 0;\n}\n",
        encoding="utf-8",
    )

    result = _subject().analyze(repo_root)

    assert isinstance(result, AnalyzerResult)
    assert result.complexity == {
        "files": {
            "src/choose.js": {
                "functions": [{"name": "choose", "ccn": 2, "start_line": 1}],
                "total_ccn": 2,
                "max_ccn": 2,
            },
            "src/choose.py": {
                "functions": [{"name": "choose", "ccn": 2, "start_line": 1}],
                "total_ccn": 2,
                "max_ccn": 2,
            },
        },
        "functions": 2,
        "total_ccn": 4,
        "max_ccn": 2,
    }


def test_missing_metrics_extra_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subject = _subject()

    def missing_extra(_module: str, *, extra: str) -> ModuleType:
        assert extra == "metrics"
        raise OptionalDependencyError("install pip install 'nava-rebar[metrics]'")

    monkeypatch.setattr(subject, "guard_import", missing_extra)

    result = subject.analyze(tmp_path)

    assert result == Unavailable(
        reason="install pip install 'nava-rebar[metrics]'",
        accruing_since="2026-01-01T00:00:00+00:00",
    )


def test_empty_analysis_is_unavailable_not_a_confident_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty lizard analysis yields unavailable because zero requires measured files."""

    subject = _subject()
    fake_lizard = SimpleNamespace(analyze=lambda _paths: [])
    monkeypatch.setattr(subject, "guard_import", lambda *_args, **_kwargs: fake_lizard)

    result = subject.analyze(tmp_path)

    assert isinstance(result, Unavailable)
    assert "lizard" in result.reason
    assert result.accruing_since == "2026-01-01T00:00:00+00:00"


def test_root_without_any_language_lizard_supports_is_unavailable(tmp_path: Path) -> None:
    """An unsupported-language root yields unavailable through the installed lizard library."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "notes.md").write_text("# no functions here\n", encoding="utf-8")
    (repo_root / "data.toml").write_text("answer = 42\n", encoding="utf-8")

    result = _subject().analyze(repo_root)

    assert isinstance(result, Unavailable)
    assert "lizard" in result.reason


def test_measurable_root_never_reports_a_zero_complexity(tmp_path: Path) -> None:
    """Regression for AC3: a root that demonstrably HAS analyzable files still measures."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "choose.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n",
        encoding="utf-8",
    )

    result = _subject().analyze(repo_root)

    assert isinstance(result, AnalyzerResult)
    assert result.complexity is not None
    assert result.complexity["files"]
    assert result.complexity["functions"] > 0
    assert result.complexity["total_ccn"] > 0
    assert result.complexity["max_ccn"] > 0
