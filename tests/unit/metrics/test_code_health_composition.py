"""Happy-path contracts for one-pass code-health analyzer composition."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers import jscpd_dup, lizard_complexity, scc_loc
from rebar.metrics.registry import REGISTRY, MetricValue, evaluate

pytestmark = pytest.mark.unit


def test_structural_metrics_share_one_result_per_producer(monkeypatch, tmp_path):
    calls = {"scc": 0, "lizard": 0, "jscpd": 0}

    def analyze_scc(root: Path, scan_roots: list[str]) -> AnalyzerResult:
        calls["scc"] += 1
        assert root == tmp_path
        assert scan_roots == ["src"]
        return AnalyzerResult(loc={"files": {"src/app.py": 801}, "max_loc": 801})

    def analyze_lizard(root: Path) -> AnalyzerResult:
        calls["lizard"] += 1
        assert root == tmp_path
        return AnalyzerResult(
            complexity={
                "files": {"src/app.py": {"functions": [], "total_ccn": 3, "max_ccn": 3}},
                "functions": 1,
                "total_ccn": 3,
                "max_ccn": 3,
            }
        )

    def analyze_jscpd(root: Path) -> AnalyzerResult:
        calls["jscpd"] += 1
        assert root == tmp_path
        return AnalyzerResult(duplication={"clones": 2, "percentage": 4.5})

    monkeypatch.setattr(scc_loc, "analyze", analyze_scc)
    monkeypatch.setattr(lizard_complexity, "analyze", analyze_lizard)
    monkeypatch.setattr(jscpd_dup, "analyze", analyze_jscpd)
    specs = {spec.id: spec for spec in REGISTRY}
    expected_ids = {
        "module_size_distribution",
        "oversized_module_count",
        "complexity_summary",
        "duplication_summary",
    }
    assert expected_ids <= specs.keys()
    ctx = SimpleNamespace(
        repo_root=str(tmp_path),
        scan_roots=["src"],
        size_cap=800,
        size_near_fraction=0.1,
        analysis_cache={},
    )

    values = {metric_id: evaluate(specs[metric_id], ctx) for metric_id in expected_ids}

    assert all(isinstance(value, MetricValue) for value in values.values())
    assert values["module_size_distribution"].value["over_cap_count"] == 1
    assert values["oversized_module_count"].value == 1
    assert values["complexity_summary"].value["max_ccn"] == 3
    assert values["duplication_summary"].value == {"clones": 2, "percentage": 4.5}
    assert calls == {"scc": 1, "lizard": 1, "jscpd": 1}
