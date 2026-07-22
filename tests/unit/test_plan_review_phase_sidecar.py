"""Review-time phase survives sidecar persistence and recovery parsing."""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import sidecar


def test_sidecar_phase_parser_accepts_legacy_and_execution_payloads() -> None:
    assert sidecar.parse_review_phase_metadata({"schema": "plan_review_result_v2"}) == {
        "phase": "planning",
        "priority_floor": None,
    }
    assert sidecar.parse_review_phase_metadata(
        {
            "schema": "plan_review_result_v2",
            "review_phase": "execution",
            "priority_floor": 0.8,
        }
    ) == {"phase": "execution", "priority_floor": 0.8}


@pytest.mark.parametrize(
    "payload",
    [
        {"review_phase": None},
        {"review_phase": "planning", "priority_floor": 0.8},
        {"review_phase": "execution"},
        {"review_phase": "execution", "priority_floor": True},
        {"review_phase": "execution", "priority_floor": "0.8"},
        {"priority_floor": 0.8},
    ],
)
def test_sidecar_phase_parser_rejects_malformed_payloads(payload: dict) -> None:
    with pytest.raises(sidecar.SidecarReviewPhaseError):
        sidecar.parse_review_phase_metadata(payload)
