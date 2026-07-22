"""Held-out re-sign enforcement contracts."""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest, resign


def _payload() -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": "1111-2222-3333-4444",
        "material_fingerprint": "aaaaaaaaaaaaaaaa",
        "coverage": {},
        "reviewed_related_material": [
            {
                "role": "child",
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "material_fingerprint": "1111111111111111",
            }
        ],
    }


def test_enforced_stale_sidecar_pins_refuse_resign_without_compute_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(attest, "_read_enforce_plan_material_pins")
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: _payload())
    monkeypatch.setattr(
        attest,
        "current_material_fingerprint",
        lambda ticket_id, **k: {
            "1111-2222-3333-4444": "aaaaaaaaaaaaaaaa",
            "aaaa-bbbb-cccc-dddd": "2222222222222222",
        }[ticket_id],
    )
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    monkeypatch.setattr(
        attest,
        "compute_validity",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    monkeypatch.setattr(
        attest,
        "sign_plan_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not sign stale pins")),
    )
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is False
    assert result["verdict"] == "stale-pin-drift"


def test_legacy_sidecar_remains_resignable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert callable(getattr(resign.sidecar, "parse_reviewed_related_material", None))
    payload = _payload()
    payload.pop("reviewed_related_material")
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "a" * 16)
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    monkeypatch.setattr(
        attest, "sign_plan_review", lambda *a, **k: {"key_id": "k", "head_sha": "h"}
    )
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is True
