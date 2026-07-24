"""Held-out edge contracts for code-health analyzer composition. WITHHELD."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers import jscpd_dup, lizard_complexity, scc_loc
from rebar.metrics.registry import REGISTRY, Unavailable, evaluate

pytestmark = pytest.mark.unit


def _context(tmp_path):
    return SimpleNamespace(
        repo_root=str(tmp_path),
        scan_roots=[],
        size_cap=800,
        size_near_fraction=0.1,
        analysis_cache={},
    )


def test_unavailable_reason_survives_registry_evaluation(monkeypatch, tmp_path):
    calls = {"scc": 0, "lizard": 0, "jscpd": 0}

    def unavailable(name: str, reason: str):
        def analyze(*_args, **_kwargs):
            calls[name] += 1
            return Unavailable(reason=reason, accruing_since="2026-01-01T00:00:00+00:00")

        return analyze

    monkeypatch.setattr(scc_loc, "analyze", unavailable("scc", "could not run scc: missing"))
    monkeypatch.setattr(
        lizard_complexity,
        "analyze",
        unavailable("lizard", "optional dependency 'lizard' is missing"),
    )
    monkeypatch.setattr(
        jscpd_dup,
        "analyze",
        unavailable("jscpd", "could not run jscpd: missing"),
    )
    specs = {spec.id: spec for spec in REGISTRY}
    ctx = _context(tmp_path)

    results = {
        metric_id: evaluate(specs[metric_id], ctx)
        for metric_id in (
            "module_size_distribution",
            "oversized_module_count",
            "complexity_summary",
            "duplication_summary",
        )
    }

    assert results["module_size_distribution"].reason == "could not run scc: missing"
    assert results["oversized_module_count"].reason == "could not run scc: missing"
    assert results["complexity_summary"].reason == "optional dependency 'lizard' is missing"
    assert results["duplication_summary"].reason == "could not run jscpd: missing"
    assert calls == {"scc": 1, "lizard": 1, "jscpd": 1}


def test_cache_is_scoped_to_one_context(monkeypatch, tmp_path):
    calls = 0

    def analyze(root, scan_roots):
        nonlocal calls
        calls += 1
        return AnalyzerResult(loc={"files": {}, "max_loc": 0})

    monkeypatch.setattr(scc_loc, "analyze", analyze)
    spec = next(spec for spec in REGISTRY if spec.id == "module_size_distribution")
    first_context = _context(tmp_path)

    evaluate(spec, first_context)
    evaluate(spec, first_context)
    evaluate(spec, _context(tmp_path))

    assert calls == 2
