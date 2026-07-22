"""Happy-path contract for derived plan-material-pin health."""

from __future__ import annotations

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
