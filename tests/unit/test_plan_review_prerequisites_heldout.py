from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rebar.llm import contracts, parity
from rebar.llm.plan_review import orchestrator, passes, prerequisite_workflow_ops, sizing
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.workflow_ops import plan_review_decide
from rebar.llm.workflow.executor import StepContext


def test_prerequisite_contract_rejects_cross_record_finding_attribution() -> None:
    assert hasattr(passes, "_prerequisite_coverage_model")
    model = contracts.response_model_for("plan_review_prerequisite_coverage")
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "records": [
                    {
                        "prerequisite_id": "aaaa-bbbb-cccc-dddd",
                        "disposition": "finding",
                        "findings": [
                            {
                                "finding": "A conflict",
                                "criteria": ["prerequisite-consistency"],
                                "prerequisite_id": "bbbb-cccc-dddd-eeee",
                            }
                        ],
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "record",
    [
        {
            "prerequisite_id": "aaaa-bbbb-cccc-dddd",
            "disposition": "consistent",
            "findings": [],
            "reason_code": "evaluation-error",
        },
        {
            "prerequisite_id": "aaaa-bbbb-cccc-dddd",
            "disposition": "indeterminate",
            "findings": [{"finding": "must not survive", "criteria": []}],
            "reason_code": "evaluation-error",
        },
        {
            "prerequisite_id": "aaaa-bbbb-cccc-dddd",
            "disposition": "indeterminate",
            "findings": [],
            "reason_code": "not-a-real-reason",
        },
    ],
)
def test_prerequisite_contract_enforces_disposition_invariants(record: dict) -> None:
    assert hasattr(passes, "_prerequisite_coverage_model")
    model = contracts.response_model_for("plan_review_prerequisite_coverage")
    with pytest.raises(ValidationError):
        model.model_validate({"records": [record]})


def test_pack_prerequisite_bins_returns_singleton_oversized_without_splitting(monkeypatch) -> None:
    assert hasattr(sizing, "PrerequisiteBlock")
    assert hasattr(sizing, "pack_prerequisite_bins")
    monkeypatch.setattr(sizing, "largest_window_tokens", lambda model: 2_060)
    monkeypatch.setattr(sizing.det_floor, "est_tokens", len)
    small = sizing.PrerequisiteBlock("aaaa-bbbb-cccc-dddd", "small")
    huge = sizing.PrerequisiteBlock("bbbb-cccc-dddd-eeee", "X" * 100)

    bins, oversized = sizing.pack_prerequisite_bins(
        [huge, small],
        subject_plan="subject",
        system_prompt="system",
        model=None,
        per_block_output_tokens=10,
        headroom=1.0,
    )

    assert bins == [[small]]
    assert oversized == [huge]
    assert huge.rendered_text == "X" * 100


def test_mint_finding_id_uses_length_prefixed_prerequisite_identity() -> None:
    finding = {
        "finding": "same text",
        "criteria": ["B", "A"],
        "prerequisite_id": "aaaa-bbbb-cccc-dddd",
    }
    fields = (
        "prerequisite-finding-id-v1",
        "same text",
        "A,B",
        "aaaa-bbbb-cccc-dddd",
    )
    encoded = b"".join(
        struct.pack(">I", len(value.encode("utf-8"))) + value.encode("utf-8") for value in fields
    )
    expected = "f" + hashlib.sha256(encoded).hexdigest()[:16]

    assert orchestrator.mint_finding_id(finding) == expected
    assert orchestrator.mint_finding_id({**finding, "prerequisite_id": "bbbb-cccc-dddd-eeee"}) != (
        expected
    )


def test_prerequisite_fidelity_report_rejects_missing_and_wrong_attribution() -> None:
    assert hasattr(parity, "prerequisite_fidelity_report")
    ids = [f"p{index:03d}-aaaa-bbbb-cccc" for index in range(20)]
    baseline = [
        parity.ItemRecord(
            valid=True,
            decision="block",
            label="block",
            gold_prerequisite_id=ticket_id,
            pred_prerequisite_id=ticket_id,
        )
        for ticket_id in ids
    ]
    candidate = list(baseline)
    candidate[-2] = parity.ItemRecord(
        valid=True,
        decision="block",
        label="block",
        gold_prerequisite_id=ids[-2],
        pred_prerequisite_id=None,
    )
    candidate[-1] = parity.ItemRecord(
        valid=True,
        decision="block",
        label="block",
        gold_prerequisite_id=ids[-1],
        pred_prerequisite_id=ids[0],
    )

    report = parity.prerequisite_fidelity_report(baseline, candidate)

    assert report.passed is False
    assert report.metrics["coverage_completeness"] < 1.0
    assert report.metrics["prerequisite_attribution_error_rate"] > 0.0
    assert any("coverage" in failure for failure in report.gating_failures)
    assert any("attribution" in failure for failure in report.gating_failures)


def test_prerequisite_indeterminate_precedes_visible_det_blocker() -> None:
    context = PlanContext(
        ticket_id="aaaa-bbbb-cccc-dddd",
        ticket_type="story",
        title="Subject",
        description="Plan",
    )
    det = {"id": "det-1", "finding": "Structural defect", "criteria": ["P1"]}
    coverage = {"llm_ran": True, "prerequisite_indeterminate": True}

    verdict = orchestrator.finalize_verdict(
        context,
        {
            "blocking": [det],
            "surfaced": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
        },
        coaching=[],
        coverage=coverage,
        runner_name="fake",
        model="fake-model",
    )

    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["blocking"] == [det]
    assert verdict["coverage"]["prerequisite_indeterminate"] is True


def test_verifier_largest_window_exhaustion_preserves_input_too_large_reason() -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    finding = {
        "finding": "The plans conflict.",
        "criteria": ["prerequisite-consistency"],
        "prerequisite_id": prerequisite_id,
    }
    ctx = StepContext(
        run_id="run",
        step_id="decide",
        kind="scripted",
        step={},
        inputs={
            "findings": [],
            "verifications": [],
            "det_blocking": [],
            "det_advisory": [],
            "review_phase": "planning",
            "has_prerequisites": True,
            "prerequisite_coverage": [
                {
                    "prerequisite_id": prerequisite_id,
                    "disposition": "finding",
                    "findings": [finding],
                }
            ],
            "prerequisite_findings": [finding],
            "prerequisite_verifications": [],
            "prerequisite_input_too_large_ids": [prerequisite_id],
        },
        workflow={},
        target_ticket="subject-ticket",
    )

    result = plan_review_decide(ctx)

    assert result["prerequisite_coverage"] == [
        {
            "prerequisite_id": prerequisite_id,
            "disposition": "indeterminate",
            "findings": [],
            "reason_code": "evaluation-error",
            "detail": "input-too-large",
        }
    ]


def test_verifier_input_builder_reports_unsplittable_whole_pair(monkeypatch) -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    monkeypatch.setattr(
        prerequisite_workflow_ops,
        "resolve_gate_config",
        lambda repo_root: SimpleNamespace(repo_path=repo_root, model="largest-model"),
    )
    monkeypatch.setattr(prerequisite_workflow_ops.prompts, "get_prompt", lambda *a, **k: object())
    monkeypatch.setattr(
        prerequisite_workflow_ops.prompts,
        "resolve_prompt",
        lambda *a, **k: ("system", "instructions"),
    )
    monkeypatch.setattr(
        sizing,
        "pack_prerequisite_verifier_bins",
        lambda records, **kwargs: ([], list(records)),
    )
    ctx = StepContext(
        run_id="run",
        step_id="focused-inputs",
        kind="scripted",
        step={},
        inputs={
            "subject_plan": "subject plan",
            "findings": [
                {
                    "finding": "The plans conflict.",
                    "criteria": ["prerequisite-consistency"],
                    "prerequisite_id": prerequisite_id,
                }
            ],
            "prerequisites": [
                {"canonical_id": prerequisite_id, "rendered_text": "prerequisite plan"}
            ],
        },
        workflow={},
        repo_root="/repo",
    )

    result = prerequisite_workflow_ops.plan_review_prerequisite_verify_inputs(ctx)

    assert result == {
        "plan": "subject plan",
        "instructions": ["No focused findings; return an empty verifications array."],
        "input_too_large_ids": [prerequisite_id],
    }
