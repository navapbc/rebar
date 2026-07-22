from __future__ import annotations

from rebar.llm import contracts, parity
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import orchestrator, passes, prerequisites, sizing
from rebar.llm.plan_review.workflow_ops import plan_review_decide
from rebar.llm.workflow.executor import StepContext
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


def test_prerequisite_fidelity_report_rejects_comparative_regression() -> None:
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
    candidate[-2:] = [
        parity.ItemRecord(
            valid=True,
            decision="advisory",
            label="block",
            gold_prerequisite_id=ticket_id,
            pred_prerequisite_id=ticket_id,
        )
        for ticket_id in ids[-2:]
    ]

    report = parity.prerequisite_fidelity_report(
        baseline,
        candidate,
        min_recall=0.80,
        baseline_recall=1.0,
        baseline_false_accept=0.0,
    )

    assert report.passed is False
    assert any("baseline recall" in failure for failure in report.gating_failures)


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


def test_mint_finding_id_different_prerequisite_ids_produce_distinct_ids() -> None:
    finding = {"finding": "same text", "criteria": ["prerequisite-consistency"]}
    first = orchestrator.mint_finding_id({**finding, "prerequisite_id": "aaaa-bbbb-cccc-dddd"})
    second = orchestrator.mint_finding_id({**finding, "prerequisite_id": "bbbb-cccc-dddd-eeee"})
    assert first != second


def test_indeterminate_coverage_records_have_no_findings() -> None:
    model = contracts.response_model_for("plan_review_prerequisite_coverage")
    parsed = model.model_validate(
        {
            "records": [
                {
                    "prerequisite_id": "aaaa-bbbb-cccc-dddd",
                    "disposition": "indeterminate",
                    "findings": [],
                    "reason_code": "evaluation-error",
                }
            ]
        }
    )
    assert parsed.records[0].findings == []


def test_batching_never_splits_subject_or_prerequisite_block(monkeypatch) -> None:
    monkeypatch.setattr(sizing, "largest_window_tokens", lambda model: 2_060)
    monkeypatch.setattr(sizing.det_floor, "est_tokens", len)
    subject = "subject remains whole"
    small = sizing.PrerequisiteBlock("aaaa-bbbb-cccc-dddd", "small block")
    oversized = sizing.PrerequisiteBlock("bbbb-cccc-dddd-eeee", "X" * 100)

    bins, rejected = sizing.pack_prerequisite_bins(
        [oversized, small],
        subject_plan=subject,
        system_prompt="system",
        model=None,
        per_block_output_tokens=10,
        headroom=1.0,
    )

    assert bins == [[small]]
    assert rejected == [oversized]
    assert small.rendered_text == "small block"
    assert oversized.rendered_text == "X" * 100
    assert subject == "subject remains whole"


def test_largest_window_exhaustion_emits_indeterminate(monkeypatch) -> None:
    oversized = sizing.PrerequisiteBlock("aaaa-bbbb-cccc-dddd", "too large")
    monkeypatch.setattr(
        sizing,
        "pack_prerequisite_bins",
        lambda blocks, **kwargs: ([], [oversized]),
    )

    records, findings = prerequisites.run_focused_finder(
        runner=object(),
        cfg=LLMConfig(runner="fake"),
        subject_plan="subject",
        blocks=[
            {
                "canonical_id": oversized.canonical_id,
                "rendered_text": oversized.rendered_text,
            }
        ],
    )

    assert findings == []
    assert records == [
        {
            "prerequisite_id": oversized.canonical_id,
            "disposition": "indeterminate",
            "findings": [],
            "reason_code": "evaluation-error",
            "detail": "input-too-large",
        }
    ]


def _valid_verification(index: int, *, attribution: str | None = None) -> dict:
    binary = {
        "cited_reference_accurate": "na",
        "is_verifiable": "yes",
        "evidence_entails_finding": "yes",
        "path_reachable": "yes",
        "impact_follows_necessarily": "yes",
        "no_viable_alternative_explanation": "yes",
        "no_existing_mitigation": "yes",
        "severity_claim_justified": "yes",
    }
    if attribution is not None:
        binary["prerequisite_attribution_valid"] = attribution
    return {
        "index": index,
        "severity_attributes": {
            "prod_impact": "high",
            "debt_impact": "high",
            "blast_radius": "system",
            "likelihood": "high",
            "reversibility": "hard",
        },
        "binary": binary,
    }


def test_focused_general_index_domains_independent() -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    general = {"finding": "general concern", "criteria": ["E1"]}
    focused = {
        "finding": "prerequisite conflict",
        "criteria": ["prerequisite-consistency"],
        "prerequisite_id": prerequisite_id,
    }
    ctx = StepContext(
        run_id="run",
        step_id="decide",
        kind="scripted",
        step={},
        inputs={
            "findings": [general],
            "verifications": [_valid_verification(0)],
            "det_blocking": [],
            "det_advisory": [],
            "review_phase": "planning",
            "has_prerequisites": True,
            "prerequisite_coverage": [
                {
                    "prerequisite_id": prerequisite_id,
                    "disposition": "finding",
                    "findings": [focused],
                }
            ],
            "prerequisite_findings": [focused],
            "prerequisite_verifications": [_valid_verification(0, attribution="yes")],
        },
        workflow={},
        target_ticket="subject-ticket",
    )

    result = plan_review_decide(ctx)
    decided = [*result["blocking"], *result["surfaced"]]

    assert any(item["finding"] == "general concern" for item in decided)
    assert any(
        item["finding"] == "prerequisite conflict" and item["prerequisite_id"] == prerequisite_id
        for item in decided
    )
