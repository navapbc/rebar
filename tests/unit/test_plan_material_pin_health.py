"""Happy-path contract for derived plan-material-pin health.

Held-out oracles merged from test_plan_material_pin_health_heldout.py: Held-out edge oracle for
plan-material-pin validity.
"""

from __future__ import annotations

import logging

import pytest

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin


def _derive():
    derive = getattr(attest, "derive_plan_material_pin_health", None)
    assert callable(derive), "derive_plan_material_pin_health API is absent"
    return derive


def test_current_pins_have_exact_in_band_health(monkeypatch: pytest.MonkeyPatch) -> None:
    pins = (
        PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111"),
        PlanMaterialPin("prerequisite", "eeee-ffff-aaaa-bbbb", "2222222222222222"),
    )
    monkeypatch.setattr(
        attest,
        "current_material_fingerprint",
        lambda ticket_id, repo_root=None: {
            "aaaa-bbbb-cccc-dddd": "1111111111111111",
            "eeee-ffff-aaaa-bbbb": "2222222222222222",
        }[ticket_id],
    )

    assert _derive()(pins, repo_root="/repo", enforced=True) == {
        "pin_status": "current",
        "enforced": True,
        "targets": [
            {
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "role": "child",
                "pinned_fingerprint": "1111111111111111",
                "current_fingerprint": "1111111111111111",
                "pin_status": "current",
            },
            {
                "canonical_id": "eeee-ffff-aaaa-bbbb",
                "role": "prerequisite",
                "pinned_fingerprint": "2222222222222222",
                "current_fingerprint": "2222222222222222",
                "pin_status": "current",
            },
        ],
    }


@pytest.mark.parametrize("records", [None, ()])
def test_wholly_unpinned_material_is_legacy_compatible(records) -> None:
    assert _derive()(records, repo_root="/repo", enforced=True) == {
        "pin_status": "legacy-unpinned",
        "enforced": True,
        "targets": [],
    }


def test_deleted_material_is_missing_but_archived_material_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar import _reads

    state = {
        "ticket_id": "aaaa-bbbb-cccc-dddd",
        "ticket_type": "task",
        "title": "Pinned target",
        "description": "Material used by a signed plan review.",
        "status": "archived",
        "archived": True,
    }
    monkeypatch.setattr(_reads, "show_ticket", lambda *args, **kwargs: state)
    monkeypatch.setattr(_reads, "list_tickets", lambda *args, **kwargs: [])

    archived = attest.current_material_fingerprint("aaaa-bbbb-cccc-dddd", repo_root="/repo")
    assert archived is not None

    state["status"] = "deleted"

    assert attest.current_material_fingerprint("aaaa-bbbb-cccc-dddd", repo_root="/repo") is None


# ── held-out oracles (merged from test_plan_material_pin_health_heldout.py) ──


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


def test_close_profile_disables_only_code_freshness(
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
    assert default["verdict"] == "stale-head"
    assert close["valid"] is True
    # CLOSE waives code freshness only — the pin health it reports is the same record DEFAULT
    # computed, so nothing beyond the head-drift check is suppressed.
    assert close["health"] == default["health"]


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
