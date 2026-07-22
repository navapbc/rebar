"""Signed manifest grammar for planning and execution reviews."""

from __future__ import annotations

import math

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.manifest import ManifestFormatError


def _verdict() -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": "1111-2222-3333-4444",
        "model": "m",
        "runner": "r",
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 1}},
    }


def test_build_manifest_records_phase_metadata_in_fixed_position() -> None:
    planning = attest.build_manifest(_verdict(), material="m", review_phase="planning")
    execution = attest.build_manifest(
        _verdict(), material="m", review_phase="execution", priority_floor=0.8
    )
    advisory = planning.index("advisory: 1")
    assert planning[advisory + 1 : advisory + 3] == [
        "review-phase: planning",
        planning[advisory + 2],
    ]
    assert planning[advisory + 2].startswith("rebar-version:")
    advisory = execution.index("advisory: 1")
    assert execution[advisory + 1 : advisory + 4] == [
        "review-phase: execution",
        "priority-floor: 0.80",
        execution[advisory + 3],
    ]
    assert execution[advisory + 3].startswith("rebar-version:")


def test_manifest_readers_preserve_legacy_planning_compatibility() -> None:
    legacy = attest.build_manifest(_verdict(), material="m")
    assert attest.manifest_review_phase(legacy) == "planning"
    assert attest.manifest_priority_floor(legacy) is None


@pytest.mark.parametrize(
    ("phase", "floor"),
    [
        ("planning", None),
        ("execution", 0.8),
        ("execution", 1),
    ],
)
def test_phase_metadata_helper_accepts_only_policy_valid_pairs(phase: str, floor: object) -> None:
    result = attest.validate_review_phase_metadata(phase, floor, legacy_absent=False)
    assert result["phase"] == phase
    assert result["priority_floor"] == floor


@pytest.mark.parametrize(
    ("phase", "floor"),
    [
        ("planning", 0.8),
        ("execution", None),
        ("execution", True),
        ("execution", "0.8"),
        ("execution", math.nan),
        ("execution", -0.01),
        ("execution", 1.01),
        ("unknown", None),
        (None, 0.8),
    ],
)
def test_phase_metadata_helper_rejects_malformed_pairs(phase: object, floor: object) -> None:
    with pytest.raises(ManifestFormatError):
        attest.validate_review_phase_metadata(phase, floor, legacy_absent=False)


@pytest.mark.parametrize(
    "manifest",
    [
        [
            "plan-review: PASS",
            "review-phase: execution",
            "review-phase: execution",
            "priority-floor: 0.80",
        ],
        ["plan-review: PASS", "review-phase: execution extra", "priority-floor: 0.80"],
        ["plan-review: PASS", "priority-floor: 0.80"],
        [
            "plan-review: PASS",
            "review-phase: execution",
            "priority-floor: 0.80",
            "priority-floor: 0.90",
        ],
    ],
)
def test_manifest_reader_rejects_duplicate_or_bad_tokens(manifest: list[str]) -> None:
    with pytest.raises(ManifestFormatError):
        attest.manifest_review_phase(manifest)
