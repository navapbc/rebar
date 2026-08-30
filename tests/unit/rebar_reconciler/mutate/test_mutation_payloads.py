"""Contract tests for ``rebar_reconciler.mutation_payloads`` (ADR 0107, e9d5).

Covers the ten live ``(direction, action)`` payload dataclasses: happy-path
construction + round-trip through ``as_legacy_dict()``, and validation
(missing required fields, wrong-action/extra fields, the two dead-by-design
inbound combinations). These are pure, side-effect-free unit tests — no
client, no store, no subprocess, no clock.
"""

from __future__ import annotations

import pytest

from rebar_reconciler import mutation_payloads as mp

# ---------------------------------------------------------------------------
# (outbound, create)
# ---------------------------------------------------------------------------


def test_outbound_create_round_trips_legacy_shape():
    legacy = {
        "title": "New Issue",
        "priority": "High",
        "comments": [{"body": "hi"}],
        "labels": [{"action": "add", "label": "team-a"}],
        "local_id": "abc123",
    }
    typed = mp.build_typed_payload("outbound", "create", legacy)
    assert isinstance(typed, mp.OutboundCreatePayload)
    assert typed.fields == {"title": "New Issue", "priority": "High"}
    assert typed.as_legacy_dict() == legacy


def test_outbound_create_open_ended_fields_allow_arbitrary_keys():
    typed = mp.build_typed_payload("outbound", "create", {"any_vendor_field": 1})
    assert typed.as_legacy_dict() == {"any_vendor_field": 1, "comments": [], "labels": []}


def test_outbound_create_is_a_mapping():
    typed = mp.OutboundCreatePayload(fields={"title": "x"})
    assert dict(typed) == {"title": "x", "comments": [], "labels": []}
    assert typed["title"] == "x"
    assert len(typed) == 3


# ---------------------------------------------------------------------------
# (outbound, update)
# ---------------------------------------------------------------------------


def test_outbound_update_round_trips_legacy_shape():
    legacy = {
        "changed_fields": {"status": "Done"},
        "comments": [],
        "labels": [],
        "links": [{"type": "blocks", "target": "ABC-1"}],
    }
    typed = mp.build_typed_payload("outbound", "update", legacy)
    assert isinstance(typed, mp.OutboundUpdatePayload)
    assert typed.as_legacy_dict() == legacy


def test_outbound_update_requires_at_least_one_nonempty_field():
    with pytest.raises(ValueError, match="at least one"):
        mp.OutboundUpdatePayload()


def test_outbound_update_rejects_unrecognized_field():
    with pytest.raises(ValueError, match="unrecognized"):
        mp.build_typed_payload("outbound", "update", {"changed_fields": {"x": 1}, "bogus": True})


# ---------------------------------------------------------------------------
# (outbound, delete) / (outbound, probe) — no fields at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "cls"), [("delete", mp.OutboundDeletePayload), ("probe", mp.OutboundProbePayload)]
)
def test_outbound_delete_and_probe_accept_empty_payload(action, cls):
    typed = mp.build_typed_payload("outbound", action, {})
    assert isinstance(typed, cls)
    assert typed.as_legacy_dict() == {}


@pytest.mark.parametrize("action", ["delete", "probe"])
def test_outbound_delete_and_probe_reject_any_field(action):
    """ADR 0107 defect #2: a stray field on a (outbound, delete/probe) payload
    must be rejected at construction, not silently ignored by the leaf."""
    with pytest.raises(ValueError):
        mp.build_typed_payload("outbound", action, {"changed_fields": {"x": 1}})


# ---------------------------------------------------------------------------
# (outbound, conflict)
# ---------------------------------------------------------------------------


def test_outbound_conflict_round_trips_legacy_shape():
    legacy = {"reason": "ambiguous local binding", "local_id": "abc123"}
    typed = mp.build_typed_payload("outbound", "conflict", legacy)
    assert typed.as_legacy_dict() == legacy


def test_outbound_conflict_requires_reason():
    with pytest.raises(ValueError, match="reason"):
        mp.build_typed_payload("outbound", "conflict", {})


# ---------------------------------------------------------------------------
# (inbound, create)
# ---------------------------------------------------------------------------


def test_inbound_create_round_trips_flat_legacy_shape():
    legacy = {"title": "Jira Issue", "status": "In Progress"}
    typed = mp.build_typed_payload("inbound", "create", legacy)
    assert isinstance(typed, mp.InboundCreatePayload)
    assert typed.fields == {"title": "Jira Issue"}
    assert typed.status == "In Progress"
    assert typed.as_legacy_dict() == legacy


def test_inbound_create_allows_empty_fields():
    """An inbound create with every field excluded is still meaningful
    (differ.py: 'the target itself is the signal') — payload may be empty."""
    typed = mp.build_typed_payload("inbound", "create", {})
    assert typed.as_legacy_dict() == {}


# ---------------------------------------------------------------------------
# (inbound, update)
# ---------------------------------------------------------------------------


def test_inbound_update_round_trips_run_differs_shape():
    legacy = {
        "local_id": "abc123",
        "fields": {"summary": "x"},
        "labels": [],
        "comments": [],
        "links": [],
    }
    typed = mp.build_typed_payload("inbound", "update", legacy)
    assert isinstance(typed, mp.InboundUpdatePayload)
    assert typed.as_legacy_dict() == legacy


def test_inbound_update_local_id_may_be_none():
    legacy = {"local_id": None, "fields": {}, "labels": [], "comments": [], "links": []}
    typed = mp.build_typed_payload("inbound", "update", legacy)
    assert typed.as_legacy_dict() == legacy


def test_inbound_update_rejects_unrecognized_field():
    with pytest.raises(ValueError, match="unrecognized"):
        mp.build_typed_payload("inbound", "update", {"bogus": 1})


# ---------------------------------------------------------------------------
# (inbound, clean_label)
# ---------------------------------------------------------------------------


def test_inbound_clean_label_round_trips():
    legacy = {"labels_to_remove": ["rebar-id-1", "rebar-id-2"]}
    typed = mp.build_typed_payload("inbound", "clean_label", legacy)
    assert typed.as_legacy_dict() == legacy


def test_inbound_clean_label_requires_nonempty():
    with pytest.raises(ValueError):
        mp.build_typed_payload("inbound", "clean_label", {"labels_to_remove": []})
    with pytest.raises(ValueError):
        mp.InboundCleanLabelPayload(labels_to_remove=())


# ---------------------------------------------------------------------------
# (inbound, repair_property)
# ---------------------------------------------------------------------------


def test_inbound_repair_property_round_trips_invariants_shape():
    legacy = {"local_id": "abc123"}
    typed = mp.build_typed_payload("inbound", "repair_property", legacy)
    assert typed.as_legacy_dict() == legacy


def test_inbound_repair_property_requires_local_id():
    with pytest.raises(ValueError):
        mp.build_typed_payload("inbound", "repair_property", {})


# ---------------------------------------------------------------------------
# (inbound, conflict)
# ---------------------------------------------------------------------------


def test_inbound_conflict_round_trips_adr_decided_shape():
    legacy = {"reason": "dangling_jira_local_id", "jira_key": "ABC-1", "local_id": "xyz"}
    typed = mp.build_typed_payload("inbound", "conflict", legacy)
    assert typed.as_legacy_dict() == legacy


def test_inbound_conflict_requires_reason_and_jira_key():
    with pytest.raises(ValueError):
        mp.build_typed_payload("inbound", "conflict", {})
    with pytest.raises(ValueError):
        mp.build_typed_payload("inbound", "conflict", {"reason": "x"})
    with pytest.raises(ValueError):
        mp.build_typed_payload("inbound", "conflict", {"jira_key": "ABC-1"})


# ---------------------------------------------------------------------------
# Dead-by-design combinations (ADR 0028 / bug 3b5f) — left alone, not modeled.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["delete", "probe"])
def test_inbound_delete_and_probe_are_unregistered(action):
    """(inbound, delete)/(inbound, probe) are dead-by-design (ADR 0028, bug
    3b5f): typed_dispatch._LEAVES never registers them and neither is ever
    constructed in production. This story deliberately does NOT model them —
    see e9d5's boundary note; deletion of the stale _VALID_COMBINATIONS
    allowance is bug 3b5f's job, not this one's."""
    with pytest.raises(mp.UnknownMutationKindError):
        mp.build_typed_payload("inbound", action, {})
    with pytest.raises(mp.UnknownMutationKindError):
        mp.payload_type_for("inbound", action)


def test_unknown_combination_raises():
    with pytest.raises(mp.UnknownMutationKindError):
        mp.build_typed_payload("outbound", "clean_label", {})


# ---------------------------------------------------------------------------
# as_legacy_dict() helper — typed and raw-dict payloads alike
# ---------------------------------------------------------------------------


def test_as_legacy_dict_helper_handles_typed_and_raw():
    typed = mp.OutboundDeletePayload()
    assert mp.as_legacy_dict(typed) == {}
    assert mp.as_legacy_dict({"a": 1}) == {"a": 1}
