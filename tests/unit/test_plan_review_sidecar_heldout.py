"""Held-out malformed and producer wiring cases for reviewed material."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rebar.llm.plan_review as plan_review
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import sidecar, xcheck
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin, PlanRelationSnapshot


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
        plan_review.orchestrator,
        "assemble_context",
        lambda *a, **k: SimpleNamespace(ticket_id="1111-2222-3333-4444", ticket_type="story"),
    )
    monkeypatch.setattr(plan_review.orchestrator, "material_fingerprint", lambda ctx: "m" * 16)
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

    def capture(verdict, *, material=None, reviewed_related_material=None, repo_root=None):
        captured.update(
            material=material,
            reviewed_related_material=reviewed_related_material,
            repo_root=repo_root,
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
        "material": "m" * 16,
        "reviewed_related_material": snapshot.related_material,
        "repo_root": "/repo",
    }
