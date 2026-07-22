"""Observable Pass-3 policy difference between planning and execution reviews."""

from __future__ import annotations

from typing import Any

from rebar.llm.plan_review import orchestrator


def _finding(criteria: str = "T1") -> dict[str, Any]:
    return {
        "criteria": criteria,
        "finding": "specific defect",
        "suggested_fix": "fix it",
        "citations": [],
        "evidence": [],
        "scenarios": [],
    }


def _verification(priority: float) -> dict[int, dict[str, Any]]:
    return {
        0: {
            "severity_attributes": {
                "scope": priority,
                "blast_radius": priority,
                "detectability": priority,
                "recoverability": priority,
                "data_integrity": priority,
                "security": priority,
                "availability": priority,
            },
            "binary": {"valid": True},
        }
    }


def test_execution_review_floors_blocking_threshold_without_hiding_finding(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator._criteria,
        "threshold_for",
        lambda criteria, descriptors, *, gate: (0.65, True),
    )
    thresholds = []

    def fake_kernel(findings, verifs, *, threshold_for, impact_fn):
        threshold, enabled = threshold_for("T1")
        thresholds.append((threshold, enabled))
        return [{**findings[0], "block_threshold": threshold, "blocking_enabled": enabled}]

    monkeypatch.setattr(orchestrator.review_kernel, "pass3_over_findings", fake_kernel)
    planning = orchestrator.pass3_over_findings([_finding()], _verification(0.72))
    execution = orchestrator.pass3_over_findings(
        [_finding()], _verification(0.72), execution_review=True
    )
    assert thresholds == [(0.65, True), (0.8, True)]
    assert planning[0]["finding"] == execution[0]["finding"] == "specific defect"


def test_execution_review_does_not_enable_advisory_only_criterion(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator._criteria,
        "threshold_for",
        lambda criteria, descriptors, *, gate: (0.55, False),
    )
    result = orchestrator.pass3_over_findings(
        [_finding()], _verification(0.9), execution_review=True
    )
    assert result[0]["blocking_enabled"] is False
    assert result[0]["block_threshold"] == 0.55
    assert result[0]["decision"] != "block"
