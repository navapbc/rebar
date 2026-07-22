from __future__ import annotations

from rebar.llm import contracts, parity
from rebar.llm.plan_review import orchestrator, passes, sizing
from rebar.llm.workflow.runners import BatchRunRequest


def _record(ticket_id: str, *, disposition: str = "consistent") -> dict:
    return {
        "prerequisite_id": ticket_id,
        "disposition": disposition,
        "findings": [],
    }


def test_prerequisite_coverage_contract_accepts_each_closed_disposition() -> None:
    assert hasattr(passes, "_prerequisite_coverage_model")
    model = contracts.response_model_for("plan_review_prerequisite_coverage")
    parsed = model.model_validate(
        {
            "records": [
                _record("aaaa-bbbb-cccc-dddd"),
                {
                    "prerequisite_id": "bbbb-cccc-dddd-eeee",
                    "disposition": "finding",
                    "findings": [
                        {
                            "finding": "The subject contradicts this prerequisite.",
                            "criteria": ["prerequisite-consistency"],
                            "prerequisite_id": "bbbb-cccc-dddd-eeee",
                        }
                    ],
                },
                {
                    "prerequisite_id": "cccc-dddd-eeee-ffff",
                    "disposition": "indeterminate",
                    "findings": [],
                    "reason_code": "evaluation-error",
                    "detail": "provider unavailable",
                },
            ]
        }
    ).model_dump()

    assert [record["disposition"] for record in parsed["records"]] == [
        "consistent",
        "finding",
        "indeterminate",
    ]
    assert parsed["records"][1]["findings"][0]["prerequisite_id"] == ("bbbb-cccc-dddd-eeee")


def test_pack_prerequisite_bins_orders_and_preserves_whole_blocks(monkeypatch) -> None:
    assert hasattr(sizing, "PrerequisiteBlock")
    assert hasattr(sizing, "pack_prerequisite_bins")
    monkeypatch.setattr(sizing, "largest_window_tokens", lambda model: 10_000)
    monkeypatch.setattr(sizing.det_floor, "est_tokens", len)
    blocks = [
        sizing.PrerequisiteBlock("cccc-dddd-eeee-ffff", "C" * 20),
        sizing.PrerequisiteBlock("aaaa-bbbb-cccc-dddd", "A" * 30),
        sizing.PrerequisiteBlock("bbbb-cccc-dddd-eeee", "B" * 25),
    ]

    bins, oversized = sizing.pack_prerequisite_bins(
        blocks,
        subject_plan="subject",
        system_prompt="system",
        model="test-model",
        per_block_output_tokens=10,
        headroom=1.0,
    )

    assert oversized == []
    assert [[block.canonical_id for block in bin_] for bin_ in bins] == [
        [
            "aaaa-bbbb-cccc-dddd",
            "bbbb-cccc-dddd-eeee",
            "cccc-dddd-eeee-ffff",
        ]
    ]
    assert [block.rendered_text for block in bins[0]] == ["A" * 30, "B" * 25, "C" * 20]


def test_prerequisite_fidelity_report_passes_complete_exact_attribution() -> None:
    assert hasattr(parity, "prerequisite_fidelity_report")
    baseline = []
    candidate = []
    for index in range(20):
        ticket_id = f"p{index:03d}-aaaa-bbbb-cccc"
        baseline.append(
            parity.ItemRecord(
                valid=True,
                decision="block",
                label="block",
                gold_prerequisite_id=ticket_id,
                pred_prerequisite_id=ticket_id,
            )
        )
        candidate.append(
            parity.ItemRecord(
                valid=True,
                decision="block",
                label="block",
                gold_prerequisite_id=ticket_id,
                pred_prerequisite_id=ticket_id,
            )
        )

    report = parity.prerequisite_fidelity_report(baseline, candidate)

    assert report.passed is True
    assert report.metrics["coverage_completeness"] == 1.0
    assert report.metrics["prerequisite_attribution_error_rate"] == 0.0
    assert report.metrics["min_recall"] == 0.90
    assert report.metrics["max_false_accept"] == 0.10


def test_batch_run_request_old_callers_default_to_empty_with_inputs() -> None:
    request = BatchRunRequest(
        finder="finder",
        criteria=(),
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=None,
        run_id="run",
        step_id="step",
    )

    assert request.with_inputs == {}


def test_mint_finding_id_keeps_legacy_identity_without_prerequisite() -> None:
    finding = {"finding": "same text", "criteria": ["B", "A"]}
    assert orchestrator.mint_finding_id(finding) == "f9efa64a782b331fc"
