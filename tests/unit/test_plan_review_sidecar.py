"""Reviewed-related-material producer/consumer contract.

Held-out oracles merged from test_plan_review_sidecar_heldout.py: Held-out malformed and producer
wiring cases for reviewed material.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar.llm.plan_review as plan_review
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import sidecar, xcheck
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin, PlanRelationSnapshot


def test_related_material_round_trips_exactly_without_schema_bump() -> None:
    pins = (
        PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111"),
        PlanMaterialPin("prerequisite", "eeee-ffff-aaaa-bbbb", "2222222222222222"),
    )
    payload = sidecar.build_payload(
        {"verdict": "PASS", "ticket_id": "1111-2222-3333-4444", "coverage": {}},
        material="3333333333333333",
        reviewed_related_material=pins,
    )

    assert payload["schema"] == "plan_review_result_v2"
    assert payload["reviewed_related_material"] == [
        {
            "role": "child",
            "canonical_id": "aaaa-bbbb-cccc-dddd",
            "material_fingerprint": "1111111111111111",
        },
        {
            "role": "prerequisite",
            "canonical_id": "eeee-ffff-aaaa-bbbb",
            "material_fingerprint": "2222222222222222",
        },
    ]
    assert sidecar.parse_reviewed_related_material(payload) == pins


def test_absent_related_material_is_the_only_legacy_unpinned_shape() -> None:
    assert sidecar.parse_reviewed_related_material({"schema": "plan_review_result_v2"}) is None


def test_v2_reader_preserves_legacy_payload_when_additive_material_is_present(
    tmp_path: Path,
) -> None:
    ticket_id = "1111-2222-3333-4444"
    ticket_dir = tmp_path / ".tickets-tracker" / ticket_id
    ticket_dir.mkdir(parents=True)
    legacy_payload = {
        "schema": "plan_review_result_v2",
        "ticket_id": ticket_id,
        "verdict": "PASS",
        "material_fingerprint": "3333333333333333",
        "coverage": {"counts": {"blocking": 0}},
    }
    additive = [
        {
            "role": "child",
            "canonical_id": "aaaa-bbbb-cccc-dddd",
            "material_fingerprint": "1111111111111111",
        }
    ]
    stored_payload = {**legacy_payload, "reviewed_related_material": additive}
    (ticket_dir / "100-REVIEW_RESULT.json").write_text(
        json.dumps({"data": stored_payload}), encoding="utf-8"
    )

    read_payload = sidecar.latest_review_result(ticket_id, repo_root=tmp_path)

    assert read_payload is not None
    assert {
        key: value for key, value in read_payload.items() if key != "reviewed_related_material"
    } == legacy_payload
    assert read_payload["reviewed_related_material"] == additive


# ── decision-margin telemetry (story fixable-angular-caribou, C11 P0.2) ──────────────────
def test_sidecar_records_decision_margin_against_the_real_block_line() -> None:
    """The margin is measured on PRIORITY, not impact: pass3_decide blocks on
    `priority >= block_threshold`, so an impact-based margin would not describe the boundary
    the decision actually used."""
    from rebar.llm.plan_review import sidecar

    verdict = {
        "verdict": "BLOCK",
        "ticket_id": "T-margin",
        "blocking": [
            {"finding": "over", "criteria": ["G6"], "priority": 0.72, "block_threshold": 0.6}
        ],
        "advisory": [
            {"finding": "under", "criteria": ["E5"], "priority": 0.40, "block_threshold": 0.95}
        ],
    }
    findings = {f["finding"]: f for f in sidecar.build_payload(verdict, material="m")["findings"]}
    # at/above the line -> positive; below -> negative. Both are priority-based.
    assert findings["over"]["decision_margin"] == 0.12
    assert findings["under"]["decision_margin"] == -0.55


def test_decision_margin_is_none_when_either_side_is_missing() -> None:
    """Absent -> None, never 0.0: an offline reader must be able to tell 'not recorded' from
    'landed exactly on the line'."""
    from rebar.llm.plan_review import sidecar

    verdict = {
        "verdict": "PASS",
        "ticket_id": "T-margin-none",
        "advisory": [
            {"finding": "no-threshold", "criteria": ["E5"], "priority": 0.4},
            {"finding": "no-priority", "criteria": ["E5"], "block_threshold": 0.6},
        ],
    }
    findings = {f["finding"]: f for f in sidecar.build_payload(verdict, material="m")["findings"]}
    assert findings["no-threshold"]["decision_margin"] is None
    assert findings["no-priority"]["decision_margin"] is None


# ── held-out oracles (merged from test_plan_review_sidecar_heldout.py) ──


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        [
            {
                "role": "parent",
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "material_fingerprint": "1" * 16,
            }
        ],
        [{"role": "child", "canonical_id": "not-an-id", "material_fingerprint": "1" * 16}],
        [{"role": "child", "canonical_id": "aaaa-bbbb-cccc-dddd", "material_fingerprint": "bad"}],
        [
            {
                "role": "child",
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "material_fingerprint": "1" * 16,
            },
            {
                "role": "child",
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "material_fingerprint": "1" * 16,
            },
        ],
    ],
)
def test_every_present_invalid_related_material_shape_is_rejected(raw) -> None:
    assert callable(getattr(sidecar, "parse_reviewed_related_material", None)), (
        "reviewed-related-material parser is absent"
    )
    error = getattr(sidecar, "ReviewedRelatedMaterialError", None)
    assert error is not None, "ReviewedRelatedMaterialError API is absent"
    with pytest.raises(error):
        sidecar.parse_reviewed_related_material({"reviewed_related_material": raw})


def test_full_review_emits_the_exact_pre_llm_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111")
    snapshot = PlanRelationSnapshot(
        subject_state={"ticket_id": "1111-2222-3333-4444"},
        ticket_states_by_id={},
        child_ids=(pin.canonical_id,),
        prerequisite_ids=(),
        related_material=(pin,),
        ticket_store_revision="a" * 40,
    )
    monkeypatch.setattr(
        plan_review.relation_snapshot,
        "collect_plan_relation_snapshot",
        lambda *a, **k: snapshot,
    )
    monkeypatch.setattr(
        plan_review.context_assembly,
        "assemble_context",
        lambda *a, **k: SimpleNamespace(ticket_id="1111-2222-3333-4444", ticket_type="story"),
    )
    monkeypatch.setattr(
        "rebar.llm.workflow.gate_dispatch.produce_plan_review_verdict",
        lambda *a, **k: {
            "verdict": "PASS",
            "ticket_id": "1111-2222-3333-4444",
            "coverage": {"llm_ran": True, "counts": {}},
            "blocking": [],
            "advisory": [],
            "overflow": [],
            "dropped": [],
        },
    )
    monkeypatch.setattr(plan_review, "_maybe_apply_rising_floor", lambda *a, **k: None)
    monkeypatch.setattr(plan_review, "_maybe_apply_completion_floor", lambda *a, **k: None)
    monkeypatch.setattr(plan_review.drift_floor, "maybe_apply", lambda *a, **k: None)
    monkeypatch.setattr(xcheck, "maybe_apply_contradiction", lambda *a, **k: None)
    monkeypatch.setattr(xcheck, "maybe_apply_comment_trail", lambda *a, **k: None)
    monkeypatch.setattr(plan_review, "_group_blocking_fix_units", lambda *a, **k: None)
    captured = {}

    def capture(
        verdict,
        *,
        material=None,
        material_parts=None,
        reviewed_related_material=None,
        review_phase=None,
        priority_floor=None,
        repo_root=None,
        source=None,
        retry_lineage=None,
    ):
        captured.update(
            source=source,
            material=material,
            reviewed_related_material=reviewed_related_material,
            review_phase=review_phase,
            priority_floor=priority_floor,
            repo_root=repo_root,
            retry_lineage=retry_lineage,
        )
        return True

    monkeypatch.setattr(sidecar, "emit", capture)
    result = plan_review._run_plan_review(
        "1111-2222-3333-4444",
        cfg=LLMConfig(),
        runner=None,
        sign=False,
        emit_sidecar=True,
        advisory_cap=None,
        repo_root="/repo",
    )
    assert result["sidecar_emitted"] is True
    assert captured == {
        "source": None,  # no resolved gate handle on a direct _run_plan_review call
        "material": plan_review.generation.from_snapshot(snapshot).own_material,
        "reviewed_related_material": snapshot.related_material,
        "review_phase": "planning",
        "priority_floor": None,
        "repo_root": "/repo",
        "retry_lineage": None,  # a normal (non-retry) review carries no retry lineage
    }
