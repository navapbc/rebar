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


def test_backstop_never_touches_llm_tier_findings() -> None:
    """The DET hygiene backstop is DET-scoped by construction: it lives inside
    det_blocking_findings / det_advisory_findings, which only ever receive DetResult objects. An
    LLM-tier finding (a plain dict flowing through the kernel pass3 path) is NEVER subjected to the
    subject check — a subject-less LLM finding is decided on validity/veto, not dropped for lacking
    a location/evidence."""
    from rebar.llm import review_kernel

    # a subject-less LLM-tier finding (no location, no evidence) with full validity + impact
    llm_finding = {"finding": "a real defect", "criteria": ["E1"], "location": "", "evidence": []}
    base_b = {q: "yes" for q in review_kernel.GRADED_BINARY}
    verifs = {
        0: {
            "binary": base_b,
            "severity_attributes": {
                "prod_impact": "high",
                "blast_radius": "system",
                "likelihood": "high",
                "reversibility": "hard",
            },
        }
    }
    decided = review_kernel.pass3_over_findings(
        [llm_finding], verifs, threshold_for=lambda c: (0.95, False)
    )
    # NOT dropped by any subject/hygiene rule — it survives as an advisory (LLM tier)
    assert decided[0]["decision"] == "advisory"
    assert decided[0]["tier"] == "LLM"
