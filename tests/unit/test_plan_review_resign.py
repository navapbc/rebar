"""Pin wiring across every plan-review signature-producing path."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from rebar.llm.plan_review import attest, resign


def _api():
    try:
        module = importlib.import_module("rebar.llm.plan_review.relation_snapshot")
    except ModuleNotFoundError:
        pytest.fail("plan relation snapshot API is absent")
    pins = (
        module.PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "0123456789abcdef"),
        module.PlanMaterialPin("prerequisite", "eeee-ffff-aaaa-bbbb", "fedcba9876543210"),
    )
    return module.PlanRelationSnapshot, pins


def _snapshot(ticket_id: str):
    PlanRelationSnapshot, pins = _api()
    return PlanRelationSnapshot(
        subject_state={"ticket_id": ticket_id},
        ticket_states_by_id={ticket_id: {"ticket_id": ticket_id}},
        child_ids=(pins[0].canonical_id,),
        prerequisite_ids=(pins[1].canonical_id,),
        related_material=pins,
        ticket_store_revision="a" * 40,
    )


def _capture_sign(monkeypatch):
    captured: list[str] = []

    def fake_sign(ticket_id, manifest, **kwargs):
        captured[:] = manifest
        return {"key_id": "key", "head_sha": "head"}

    monkeypatch.setattr("rebar.signing.sign_manifest", fake_sign)
    monkeypatch.setattr(attest, "dependency_hashes", lambda *a, **k: {})
    monkeypatch.setattr(attest, "registry_version", lambda *a, **k: "registry")
    monkeypatch.setattr("rebar.llm.plan_review.registry.disabled_builtins", lambda *a, **k: [])
    monkeypatch.setattr("rebar.llm.config.current_code_sha", lambda: None)
    monkeypatch.setattr("rebar.llm.overlap.queue.enqueue", lambda *a, **k: None)
    return captured


def test_ordinary_signing_collects_and_emits_current_pins(monkeypatch) -> None:
    _, pins = _api()
    ticket_id = "1111-2222-3333-4444"
    monkeypatch.setattr(
        "rebar.llm.plan_review.relation_snapshot.collect_plan_relation_snapshot",
        lambda *a, **k: _snapshot(ticket_id),
    )
    captured = _capture_sign(monkeypatch)
    attest.sign_plan_review(
        {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "model": "m",
            "runner": "r",
            "coverage": {"counts": {}, "llm_ran": True},
        },
        material="1111111111111111",
    )
    assert attest.manifest_pins(captured) == list(pins)


def test_drift_refresh_collects_and_emits_current_pins(monkeypatch) -> None:
    _, pins = _api()
    ticket_id = "1111-2222-3333-4444"
    monkeypatch.setattr(
        "rebar.llm.plan_review.relation_snapshot.collect_plan_relation_snapshot",
        lambda *a, **k: _snapshot(ticket_id),
    )
    captured = _capture_sign(monkeypatch)
    monkeypatch.setattr("rebar.signing.verify_signature", lambda *a, **k: {"key_id": "old"})
    monkeypatch.setattr(attest, "_rehash", lambda *a, **k: {})
    prior = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material="1111111111111111",
    )
    attest.refresh_attestation(ticket_id, prior, probe="PASS")
    assert attest.manifest_pins(captured) == list(pins)


def test_resign_routes_through_pin_collecting_sign_path(monkeypatch) -> None:
    _api()
    ticket_id = "1111-2222-3333-4444"
    payload = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "material_fingerprint": "1111111111111111",
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    generation = SimpleNamespace(own_material=payload["material_fingerprint"], phase="planning")
    monkeypatch.setattr("rebar.llm.plan_review.generation.collect", lambda *a, **k: generation)
    seen = {}

    def fake_sign(verdict, **kwargs):
        seen["ticket_id"] = verdict["ticket_id"]
        seen["generation"] = kwargs["initial_generation"]
        return {"key_id": "key", "head_sha": "head"}

    monkeypatch.setattr(attest, "sign_plan_review", fake_sign)
    result = resign.resign_plan_review(ticket_id)
    assert result["ok"] is True
    assert seen == {"ticket_id": ticket_id, "generation": generation}
