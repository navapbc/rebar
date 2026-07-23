"""Happy-path contracts for derived plan-review health on detailed surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar._cli._audit_commands import _render_text
from rebar._mcp_models import TicketStateOut
from rebar.audit import page, read

HEALTH = {
    "valid": True,
    "reason": "certified plan-review attestation",
    "verdict": "certified",
    "pin_status": "current",
    "enforced": True,
    "phase_status": "compatible",
    "signed_phase": "execution",
    "required_phase": "execution",
    "effective_execution_floor": 0.8,
    "advisory": False,
    "targets": [
        {
            "canonical_id": "aaaa-bbbb-cccc-dddd",
            "role": "prerequisite",
            "pinned_fingerprint": "0123456789abcdef",
            "current_fingerprint": "0123456789abcdef",
            "pin_status": "current",
        }
    ],
}


def test_audit_trail_derives_current_plan_review_health(monkeypatch) -> None:
    ticket = {
        "ticket_id": "1111-2222-3333-4444",
        "title": "subject",
        "status": "in_progress",
        "plan_review_phase": "execution",
        "attestations": {"plan-review": {"signed_at": 1}},
    }
    monkeypatch.setattr(read, "rebar_show", lambda *_a, **_k: ticket)
    monkeypatch.setattr(read, "_completion_attestation", lambda *_a, **_k: None)
    monkeypatch.setattr(read, "_completion_sidecar_record", lambda *_a, **_k: None)
    monkeypatch.setattr(read, "_related_code_reviews", lambda *_a, **_k: [])
    monkeypatch.setattr("rebar.llm.plan_review.sidecar.all_review_results", lambda *_a, **_k: [])

    verified = {
        "verified": True,
        "verdict": "certified",
        "opcert": True,
        "signed_manifest": ["plan-review: PASS"],
    }
    verify_calls = []
    calls = []

    def fake_verify(ticket_id, *, kind, repo_root=None):
        verify_calls.append((ticket_id, kind, repo_root))
        return verified

    def fake_compute(attestation, state, kind, *, repo_root=None):
        calls.append((attestation, state, kind, repo_root))
        return {"valid": True, "reason": HEALTH["reason"], "verdict": "certified", "health": HEALTH}

    monkeypatch.setattr("rebar.signing.verify_signature", fake_verify)
    monkeypatch.setattr("rebar.llm.plan_review.attest.compute_validity", fake_compute)
    trail = read.audit_trail(ticket["ticket_id"], repo_root="/repo")

    assert verify_calls == [(ticket["ticket_id"], "plan-review", "/repo")]
    assert calls == [(verified, ticket, "plan-review", "/repo")]
    assert trail["plan_review_health"] == HEALTH


def test_detailed_renderers_share_the_same_structured_health(capsys) -> None:
    trail = {
        "ticket": {"ticket_id": "1111-2222-3333-4444", "title": "subject"},
        "plan_reviews": [],
        "completion": None,
        "code_reviews": [],
        "plan_review_health": HEALTH,
    }

    _render_text(trail)
    rendered = capsys.readouterr().out
    assert "plan_review_health: current (enforced)" in rendered
    assert "phase: execution -> execution (compatible), floor=0.80" in rendered
    assert "aaaa-bbbb-cccc-dddd prerequisite current" in rendered

    context = page.build_context(trail)
    assert context["plan_review_health"] == HEALTH


def test_disabled_healthy_health_is_not_labeled_advisory(capsys) -> None:
    healthy_disabled = {
        **HEALTH,
        "enforced": False,
        "enforcement_status": "disabled",
        "advisory": False,
    }
    _render_text({"plan_review_health": healthy_disabled})
    rendered = capsys.readouterr().out
    assert "current (enforcement disabled)" in rendered
    assert "advisory" not in rendered


def test_health_derivation_failure_uses_canonical_compact_payload(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.compute_validity",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )

    unavailable = read.plan_review_health({})
    assert unavailable == {
        "available": False,
        "reason": "derived plan-review health unavailable",
    }

    context = page.build_context({})
    assert context["plan_review_health"] == unavailable

    model = TicketStateOut.model_validate(
        {
            "ticket_id": "1111-2222-3333-4444",
            "ticket_type": "story",
            "title": "subject",
            "status": "in_progress",
            "priority": 2,
            "plan_review_health": unavailable,
        }
    )
    assert model.model_dump()["plan_review_health"] == unavailable

    _render_text({"plan_review_health": unavailable})
    rendered = capsys.readouterr().out
    assert rendered.count("derived plan-review health unavailable") == 1


def test_renderers_infer_current_no_related_material_for_legacy_payload(
    capsys,
) -> None:
    health = {**HEALTH, "targets": [], "effective_execution_floor": None}
    trail = {
        "ticket": {"ticket_id": "1111-2222-3333-4444", "title": "subject"},
        "plan_reviews": [],
        "completion": None,
        "code_reviews": [],
        "plan_review_health": health,
    }

    _render_text(trail)
    rendered = capsys.readouterr().out
    assert "plan_review_health: current (no related material) (enforced)" in rendered
    assert "floor=" not in rendered

    context = page.build_context(trail)
    assert context["plan_review_health"] == health


def test_web_renderer_accepts_compact_unavailable_and_legacy_current_payloads() -> None:
    pytest.importorskip("jinja2")
    from rebar.audit import server

    template = server._jinja_env().get_template("ticket.html")
    unavailable_html = template.render(**page.build_context({}))
    assert "Derived plan-review health unavailable" in unavailable_html

    health = {**HEALTH, "targets": [], "effective_execution_floor": None}
    current_html = template.render(
        **page.build_context(
            {
                "ticket": {"ticket_id": "1111-2222-3333-4444", "title": "subject"},
                "plan_reviews": [],
                "completion": None,
                "code_reviews": [],
                "plan_review_health": health,
            }
        )
    )
    assert "current (no related material)" in current_html


def test_renderer_keeps_legacy_unpinned_distinct(capsys) -> None:
    health = {**HEALTH, "pin_status": "legacy-unpinned", "targets": []}
    trail = {
        "ticket": {"ticket_id": "1111-2222-3333-4444", "title": "subject"},
        "plan_reviews": [],
        "completion": None,
        "code_reviews": [],
        "plan_review_health": health,
    }

    _render_text(trail)
    rendered = capsys.readouterr().out
    assert "plan_review_health: legacy-unpinned (enforced)" in rendered
    assert "current (no related material)" not in rendered


def test_mcp_ticket_detail_schema_exposes_structured_plan_review_health() -> None:
    mcp_schema = TicketStateOut.model_json_schema()["properties"]["plan_review_health"]
    rendered_mcp_schema = json.dumps(mcp_schema, sort_keys=True)
    for field in (
        "available",
        "pin_status",
        "enforcement_status",
        "phase_status",
        "advisory",
        "targets",
        "canonical_id",
        "role",
        "pinned_fingerprint",
        "current_fingerprint",
    ):
        assert field in rendered_mcp_schema

    canonical = json.loads(
        (Path(__file__).parents[2] / "src/rebar/schemas/ticket_state.schema.json").read_text()
    )["properties"]["plan_review_health"]
    rendered_canonical = json.dumps(canonical, sort_keys=True)
    for field in ("available", "pin_status", "enforcement_status", "targets"):
        assert field in rendered_canonical
    model = TicketStateOut.model_validate(
        {
            "ticket_id": "1111-2222-3333-4444",
            "ticket_type": "story",
            "title": "subject",
            "status": "in_progress",
            "priority": 2,
            "plan_review_health": HEALTH,
        }
    )

    assert model.model_dump()["plan_review_health"] == HEALTH
