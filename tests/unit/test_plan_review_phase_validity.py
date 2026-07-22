"""Signed review-time phase is compared with current compiled phase on every read."""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.pin_health import PlanValidityProfile


def _manifest(*, phase: str | None = None, floor: float | None = None) -> list[str]:
    verdict = {
        "verdict": "PASS",
        "ticket_id": "1111-2222-3333-4444",
        "coverage": {"counts": {}},
    }
    kwargs = {}
    if phase is not None:
        kwargs = {"review_phase": phase, "priority_floor": floor}
    return attest.build_manifest(verdict, material="material", regver="registry", **kwargs)


def _result(monkeypatch, current: str, manifest: list[str]) -> dict:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "registry")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "material")
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda *a, **k: False)
    return attest.compute_validity(
        {"verified": True, "manifest": manifest, "signed_at": 2, "head_sha": "head"},
        {
            "ticket_id": "1111-2222-3333-4444",
            "status": "in_progress",
            "plan_review_phase": current,
        },
        "plan-review",
        profile=PlanValidityProfile.CLOSE,
    )


@pytest.mark.parametrize(
    ("current", "signed", "floor", "valid", "phase_status"),
    [
        ("planning", None, None, True, "compatible"),
        ("execution", None, None, True, "compatible"),
        ("planning", "planning", None, True, "compatible"),
        ("execution", "planning", None, True, "compatible"),
        ("planning", "execution", 0.8, False, "incompatible"),
        ("execution", "execution", 0.8, True, "compatible"),
        ("execution", "execution", 0.7, False, "incompatible"),
    ],
)
def test_phase_compatibility_table(
    monkeypatch, current, signed, floor, valid, phase_status
) -> None:
    result = _result(monkeypatch, current, _manifest(phase=signed, floor=floor))
    assert result["valid"] is valid
    assert result["health"]["phase_status"] == phase_status


def test_malformed_phase_is_reported_in_health(monkeypatch) -> None:
    manifest = _manifest()
    manifest.insert(7, "review-phase: execution")
    result = _result(monkeypatch, "execution", manifest)
    assert result["valid"] is False
    assert result["health"]["phase_status"] == "malformed"


def test_completion_validity_never_reads_plan_phase(monkeypatch) -> None:
    monkeypatch.setattr(
        attest, "manifest_review_phase", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    result = attest.compute_validity(
        {"verified": True, "manifest": ["completion-verifier: PASS"], "signed_at": 2},
        {"ticket_id": "1111-2222-3333-4444", "status": "closed"},
        "completion-verifier",
    )
    assert result["valid"] is True
