"""HELD-OUT edge tests for a8e5 Component 2 (DET-tier hygiene backstop). Merge into
tests/unit/test_plan_review.py after the implementer has only seen the happy path."""

import pytest

from rebar.llm.plan_review.det_floor import (
    DetResult,
    det_advisory_findings,
    det_blocking_findings,
    det_finding_has_subject,
)

pytestmark = pytest.mark.unit


def test_det_finding_with_evidence_survives() -> None:
    keep = DetResult(
        "P5",
        "p5_task_dag",
        "fail",
        blocking=False,
        finding={
            "finding": "Sibling tickets touch the same file(s) with no ordering edge.",
            "evidence": ["src/foo.py (ab12, cd34)"],
        },
    )
    out = det_advisory_findings([keep])
    assert len(out) == 1 and out[0]["criteria"] == ["P5"]


def test_det_finding_with_location_but_no_evidence_survives() -> None:
    keep = DetResult(
        "P4",
        "p4_oversize",
        "fail",
        blocking=False,
        finding={"finding": "oversize", "location": "the plan body", "evidence": []},
    )
    assert len(det_advisory_findings([keep])) == 1


def test_whitespace_only_location_is_not_a_subject() -> None:
    assert det_finding_has_subject({"finding": "x", "location": "   ", "evidence": []}) is False
    drop = DetResult(
        "P4",
        "p4_oversize",
        "fail",
        blocking=False,
        finding={"finding": "x", "location": "   ", "evidence": []},
    )
    assert det_advisory_findings([drop]) == []


def test_pass_and_abstain_results_never_emit() -> None:
    # Only fails emit; the backstop does not change that.
    passing = DetResult("P4", "p4_oversize", "pass")
    abstaining = DetResult("P2", "p2_resolution", "abstain")
    assert det_advisory_findings([passing, abstaining]) == []
    assert det_blocking_findings([passing, abstaining]) == []


def test_blocking_lane_keeps_a_subjectful_block() -> None:
    keep = DetResult(
        "P1",
        "p1_readiness_shape",
        "fail",
        blocking=True,
        finding={"finding": "no AC block", "evidence": ["No `## Acceptance Criteria` found."]},
    )
    out = det_blocking_findings([keep])
    assert len(out) == 1 and out[0]["tier"] == "DET"
