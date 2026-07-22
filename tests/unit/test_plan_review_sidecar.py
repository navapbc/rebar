"""Reviewed-related-material producer/consumer contract."""

from __future__ import annotations

from rebar.llm.plan_review import sidecar
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin


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
