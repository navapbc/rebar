"""Held-out contracts for analyzer-backed module-size metrics. WITHHELD."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.metrics import git_metrics
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import REGISTRY, Unavailable, evaluate

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def test_near_cap_boundary():
    loc = {
        "files": {"a": 720, "b": 800, "c": 719, "d": 801},
        "max_loc": 801,
    }

    assert git_metrics.module_size_distribution(loc, 800, 0.1) == {
        "count": 4,
        "near_cap_count": 2,
        "over_cap_count": 1,
        "max_loc": 801,
    }


def test_no_cap():
    loc = {"files": {"a": 720, "b": 801}, "max_loc": 801}

    assert git_metrics.module_size_distribution(loc, None, 0.1) == {
        "count": 2,
        "near_cap_count": None,
        "over_cap_count": None,
        "max_loc": 801,
    }
    oversized = getattr(git_metrics, "oversized_module_count", None)
    assert callable(oversized), "the analyzer-backed oversized metric must be public"
    assert oversized(loc, None, 0.1) is None


def test_analyzer_specs_use_context_config(monkeypatch, tmp_path):
    calls: list[tuple[Path, list[str]]] = []

    def analyze(root: Path, scan_roots: list[str]) -> AnalyzerResult:
        calls.append((root, scan_roots))
        return AnalyzerResult(loc={"files": {"src/a.py": 801}, "max_loc": 801})

    analyzer_module = getattr(git_metrics, "scc_loc", None)
    assert analyzer_module is not None, "module-size specs must use the scc adapter"
    monkeypatch.setattr(analyzer_module, "analyze", analyze)
    specs = {spec.id: spec for spec in REGISTRY}
    ctx = SimpleNamespace(
        repo_root=str(tmp_path),
        scan_roots=["src", "web"],
        size_cap=800,
        size_near_fraction=0.1,
        analysis_cache={},
    )

    distribution = evaluate(specs["module_size_distribution"], ctx)
    oversized = evaluate(specs["oversized_module_count"], ctx)

    assert distribution.value["over_cap_count"] == 1
    assert oversized.value == 1
    assert calls == [(tmp_path, ["src", "web"])]


def test_analyzer_unavailable_reason_is_preserved(monkeypatch, tmp_path):
    analyzer_module = getattr(git_metrics, "scc_loc", None)
    assert analyzer_module is not None, "module-size specs must use the scc adapter"
    monkeypatch.setattr(
        analyzer_module,
        "analyze",
        lambda *_args, **_kwargs: Unavailable(
            reason="could not run scc: [Errno 2] missing",
            accruing_since="2026-01-01T00:00:00+00:00",
        ),
    )
    spec = next(spec for spec in REGISTRY if spec.id == "module_size_distribution")
    result = evaluate(
        spec,
        SimpleNamespace(
            repo_root=str(tmp_path),
            scan_roots=[],
            size_cap=800,
            size_near_fraction=0.1,
            analysis_cache={},
        ),
    )

    assert isinstance(result, Unavailable)
    assert result.reason == "could not run scc: [Errno 2] missing"


def test_refactor_ratio_none_on_zero_insertions(tmp_path):
    # An empty/no-insertions range must return None (=> Unavailable), not raise ZeroDivisionError.
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "seed.py").write_text("x\n", encoding="utf-8")
    _commit(repo, "seed")
    # A future-only window has no commits -> zero insertions -> None.
    assert git_metrics.refactor_to_addition_ratio(str(repo), "2099-01-01", "2100-01-01") is None


def test_provenance_and_no_dup_seed():
    for metric_id in ("module_size_distribution", "oversized_module_count"):
        matches = [spec for spec in REGISTRY if spec.id == metric_id]
        assert len(matches) == 1
        assert matches[0].lens == "code_health"
        assert matches[0].source == "structural"
        assert matches[0].confidence == "high"
        assert matches[0].accruing_since == "2026-01-01T00:00:00+00:00"


def test_trend_unavailable():
    specs = {spec.id: spec for spec in REGISTRY}
    assert "cap_change_events" not in specs
    result = evaluate(specs["module_size_trend"], SimpleNamespace())
    assert isinstance(result, Unavailable)
