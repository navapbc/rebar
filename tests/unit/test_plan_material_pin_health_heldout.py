"""Held-out edge oracle for plan-material-pin validity."""

from __future__ import annotations

import logging

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin


def _derive():
    derive = getattr(attest, "derive_plan_material_pin_health", None)
    assert callable(derive), "derive_plan_material_pin_health API is absent"
    return derive


def test_enforcement_turns_drift_into_invalidity_and_disabled_is_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "2222222222222222")
    enforced = _derive()((pin,), repo_root="/repo", enforced=True)
    advisory = _derive()((pin,), repo_root="/repo", enforced=False)
    assert enforced["pin_status"] == advisory["pin_status"] == "stale-pin-drift"
    assert enforced["enforced"] is True
    assert advisory["enforced"] is False
    assert enforced["targets"][0]["current_fingerprint"] == "2222222222222222"


def test_unreadable_target_warns_and_fails_safe_without_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pin = PlanMaterialPin("prerequisite", "aaaa-bbbb-cccc-dddd", "1111111111111111")

    def unreadable(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(attest, "current_material_fingerprint", unreadable)
    with caplog.at_level(logging.WARNING):
        health = _derive()((pin,), repo_root="/repo", enforced=True)
    assert health["pin_status"] == "stale-pin-missing"
    assert health["targets"][0]["current_fingerprint"] is None
    record = next(r for r in caplog.records if getattr(r, "event", None))
    assert record.event == "plan_material_pin_target_unreadable"
    assert record.canonical_id == "aaaa-bbbb-cccc-dddd"
    assert record.failure_kind == "io"


def _validity_setup(monkeypatch: pytest.MonkeyPatch, *, target_fingerprint: str) -> dict:
    monkeypatch.setattr(attest, "registry_version", lambda *a, **k: "registry")
    monkeypatch.setattr("rebar.signing.head_sha", lambda *a, **k: "head-current")

    def fingerprint(ticket_id, repo_root=None):
        if ticket_id == "aaaa-bbbb-cccc-dddd":
            return target_fingerprint
        return "subject-material"

    monkeypatch.setattr(attest, "current_material_fingerprint", fingerprint)
    return {
        "manifest": [
            "plan-review: PASS",
            "regver: registry",
            "plan-material-pin: child aaaa-bbbb-cccc-dddd 1111111111111111",
        ],
        "head_sha": "head-current",
        "signed_at": 100,
    }


def test_compute_validity_enforces_or_advises_same_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _validity_setup(monkeypatch, target_fingerprint="2222222222222222")
    state = {"ticket_id": "subject-0000-0000-0001", "status": "open"}
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    blocked = attest.compute_validity(attestation, state, "plan-review", repo_root="/repo")
    assert blocked["valid"] is False
    assert blocked["verdict"] == "stale-pin-drift"
    assert blocked["health"]["pin_status"] == "stale-pin-drift"

    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: False)
    advisory = attest.compute_validity(attestation, state, "plan-review", repo_root="/repo")
    assert advisory["valid"] is True
    assert advisory["verdict"] == "certified"
    assert advisory["health"]["pin_status"] == "stale-pin-drift"


def test_close_and_drift_refresh_disable_only_code_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = getattr(attest, "PlanValidityProfile", None)
    assert profile is not None, "PlanValidityProfile API is absent"
    attestation = _validity_setup(monkeypatch, target_fingerprint="1111111111111111")
    attestation["head_sha"] = "old-head"
    state = {"ticket_id": "subject-0000-0000-0001", "status": "open"}
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    default = attest.compute_validity(
        attestation, state, "plan-review", repo_root="/repo", profile=profile.DEFAULT
    )
    close = attest.compute_validity(
        attestation, state, "plan-review", repo_root="/repo", profile=profile.CLOSE
    )
    refresh = attest.compute_validity(
        attestation, state, "plan-review", repo_root="/repo", profile=profile.DRIFT_REFRESH
    )
    assert default["verdict"] == "stale-head"
    assert close["valid"] is refresh["valid"] is True
    assert close["health"] == refresh["health"]


def test_completion_verifier_shape_is_byte_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hasattr(attest, "PlanValidityProfile"), "plan validity profiles are absent"
    result = attest.compute_validity(
        {"manifest": [], "signed_at": 100},
        {"ticket_id": "t", "status": "closed"},
        "completion-verifier",
    )
    assert result == {
        "valid": True,
        "reason": "certified completion-verifier attestation",
        "verdict": "certified",
    }


def test_derivation_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "x" * 16)
    writes: list[tuple] = []
    monkeypatch.setattr("rebar._commands._seam.append_event", lambda *a, **k: writes.append(a))
    _derive()((pin,), repo_root="/repo", enforced=False)
    assert writes == []
