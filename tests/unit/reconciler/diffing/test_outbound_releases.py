"""Outbound releases -> Jira fixVersions diff and plumbing (ADR 0124, volcanic-prim-parakeet)."""

from __future__ import annotations

from typing import Any

import pytest

from rebar.reconciler import inbound_differ, mutation_payloads
from rebar.reconciler.outbound_differ import OutboundMutation
from rebar.reconciler.outbound_releases import (
    _diff_releases,
    _split_releases,
    release_mutations_for,
)

pytestmark = pytest.mark.unit


def _jira(names: list[str]) -> dict[str, Any]:
    return {
        "fixVersions": [
            {"self": "s", "id": str(i), "name": n, "archived": False, "released": False}
            for i, n in enumerate(names)
        ]
    }


# ── Diff ──────────────────────────────────────────────────────────────────────


def _adds_removes(ticket, jira_fields, intent, availability=None):
    mutations, _withheld = _split_releases(ticket, jira_fields, intent, availability)
    return mutations


def test_local_release_in_intent_is_added() -> None:
    assert _adds_removes({"releases": ["2.4"]}, _jira([]), {"2.4"}) == [
        {"action": "add", "release": "2.4"}
    ]


def test_local_release_outside_intent_is_not_added() -> None:
    """An inbound-only name: re-adding it would undo a Jira-side removal (bug a06c)."""
    assert _adds_removes({"releases": ["2.4"]}, _jira([]), set()) == []


def test_jira_release_in_intent_missing_locally_is_removed() -> None:
    assert _adds_removes({"releases": []}, _jira(["2.4"]), {"2.4"}) == [
        {"action": "remove", "release": "2.4"}
    ]


def test_jira_release_rebar_never_held_is_never_removed() -> None:
    assert _adds_removes({"releases": []}, _jira(["2.4"]), set()) == []


@pytest.mark.parametrize("jira_fields", [{}, {"fixVersions": None}], ids=["absent", "none"])
def test_unobserved_fix_versions_return_none(jira_fields: dict) -> None:
    assert (
        _split_releases({"releases": ["2.4"]}, jira_fields, {"2.4"}, {"2.4": "available"}) is None
    )


def test_in_sync_yields_nothing() -> None:
    assert _split_releases({"releases": ["2.4"]}, _jira(["2.4"]), {"2.4"}) == ([], [])


# ── Withholding (ADR 0124 decisions 8, 9) ────────────────────────────────────


def test_missing_version_is_withheld_not_added() -> None:
    assert _split_releases({"releases": ["2.4"]}, _jira([]), {"2.4"}, {}) == (
        [],
        [{"release": "2.4", "reason": "missing"}],
    )


def test_archived_version_is_withheld_not_added() -> None:
    assert _split_releases({"releases": ["2.4"]}, _jira([]), {"2.4"}, {"2.4": "archived"}) == (
        [],
        [{"release": "2.4", "reason": "archived"}],
    )


def test_available_version_is_added() -> None:
    assert _split_releases({"releases": ["2.4"]}, _jira([]), {"2.4"}, {"2.4": "available"}) == (
        [{"action": "add", "release": "2.4"}],
        [],
    )


def test_remove_of_an_archived_version_is_still_emitted() -> None:
    assert _split_releases({"releases": []}, _jira(["2.4"]), {"2.4"}, {"2.4": "archived"}) == (
        [{"action": "remove", "release": "2.4"}],
        [],
    )


def test_unknown_availability_keeps_the_add() -> None:
    assert _split_releases({"releases": ["2.4"]}, _jira([]), {"2.4"}, None) == (
        [{"action": "add", "release": "2.4"}],
        [],
    )


def _config(**kw: Any) -> Any:
    from rebar.reconciler.outbound_differ import OutboundDiffConfig

    return OutboundDiffConfig(**kw)


def test_missing_intent_entry_means_no_edits() -> None:
    """Lazy first pass: no event-log evidence for this ticket -> no edits either way."""
    ticket = {"releases": ["2.4"]}
    cfg = _config(local_release_intent={"other": {"2.4"}})
    assert release_mutations_for(ticket, _jira(["2.5"]), "t-1", "REB-1", cfg) == ([], [])
    assert release_mutations_for(ticket, _jira(["2.5"]), "t-1", "REB-1", _config()) == (
        [{"action": "add", "release": "2.4"}, {"action": "remove", "release": "2.5"}],
        [],
    )


def test_sink_records_withheld_only_when_availability_is_known() -> None:
    sink: dict = {}
    cfg = _config(
        local_release_intent={"t-1": {"2.4"}},
        release_availability={"REB": {}},
        release_withheld_sink=sink,
    )
    assert release_mutations_for({"releases": ["2.4"]}, _jira([]), "t-1", "REB-1", cfg) == (
        [],
        ["2.4"],
    )
    assert sink == {"t-1": ("REB-1", [{"release": "2.4", "reason": "missing"}])}

    unknown: dict = {}
    cfg = _config(
        local_release_intent={"t-1": {"2.4"}},
        release_availability={"OTHER": {}},
        release_withheld_sink=unknown,
    )
    release_mutations_for({"releases": ["2.4"]}, _jira([]), "t-1", "REB-1", cfg)
    assert unknown == {}


def test_unobserved_snapshot_records_nothing_in_the_sink() -> None:
    """An unobserved fixVersions must not read as 'nothing withheld' and resolve alerts."""
    sink: dict = {}
    cfg = _config(
        local_release_intent={"t-1": {"2.4"}},
        release_availability={"REB": {}},
        release_withheld_sink=sink,
    )
    assert release_mutations_for({"releases": ["2.4"]}, {}, "t-1", "REB-1", cfg) == ([], [])
    assert sink == {}


def test_withheld_release_suppresses_the_inbound_remove() -> None:
    outbound = [
        OutboundMutation(
            local_id="local-1",
            jira_key="PROJ-1",
            action="update",
            fields={},
            withheld_releases=["2.4"],
        )
    ]
    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot={"PROJ-1": _jira_issue([])},
        binding_store=_Store(),
        local_tickets_by_id={"local-1": _local(["2.4"])},
        outbound_mutations=outbound,
    )
    assert suppressed == 1
    assert all(not m.releases for m in result)


# ── Payload plumbing ─────────────────────────────────────────────────────────


def test_outbound_update_payload_round_trips_releases() -> None:
    legacy = {
        "changed_fields": {},
        "comments": [],
        "labels": [],
        "links": [],
        "releases": [{"action": "add", "release": "2.4"}],
    }
    typed = mutation_payloads.build_typed_payload("outbound", "update", legacy)
    assert typed.as_legacy_dict() == legacy


def test_releases_free_payload_projects_no_releases_key() -> None:
    typed = mutation_payloads.OutboundUpdatePayload(changed_fields={"summary": "x"})
    assert "releases" not in typed.as_legacy_dict()


def test_releases_only_payload_is_valid() -> None:
    typed = mutation_payloads.OutboundUpdatePayload(
        releases=({"action": "remove", "release": "2.4"},)
    )
    assert typed.releases == ({"action": "remove", "release": "2.4"},)


def _mutation(payload: Any, action: str = "update") -> Any:
    from rebar.reconciler import applier

    mut_mod = applier._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=getattr(mut_mod.MutationAction, action),
        target="REB-1",
        payload=payload,
        provenance={"source": "test"},
    )


def test_batch_dict_carries_releases_from_a_typed_update() -> None:
    from rebar.reconciler.batch_dispatch import _mutation_to_batch_dict

    payload = mutation_payloads.OutboundUpdatePayload(
        releases=({"action": "add", "release": "2.4"},)
    )
    assert _mutation_to_batch_dict(_mutation(payload))["releases"] == [
        {"action": "add", "release": "2.4"}
    ]


def test_batch_dict_gives_a_create_payload_no_releases() -> None:
    from rebar.reconciler.batch_dispatch import _mutation_to_batch_dict

    payload = mutation_payloads.OutboundCreatePayload(fields={"title": "t"}, local_id="x")
    assert _mutation_to_batch_dict(_mutation(payload, "create"))["releases"] == []


# ── Same-pass suppression with a real OutboundMutation ────────────────────────


class _Store:
    def get_local_id(self, jira_key: str) -> str | None:
        return {"PROJ-1": "local-1"}.get(jira_key)


def _local(releases: list[str]) -> dict[str, Any]:
    return {
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "alice",
        "tags": [],
        "releases": releases,
    }


def _jira_issue(names: list[str]) -> dict[str, Any]:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "alice",
        "labels": [],
        **_jira(names),
    }


def test_real_outbound_release_add_suppresses_inbound_remove() -> None:
    outbound = [
        OutboundMutation(
            local_id="local-1",
            jira_key="PROJ-1",
            action="update",
            fields={},
            releases=[{"action": "add", "release": "2.4"}],
        )
    ]
    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot={"PROJ-1": _jira_issue([])},
        binding_store=_Store(),
        local_tickets_by_id={"local-1": _local(["2.4"])},
        outbound_mutations=outbound,
    )
    assert suppressed == 1
    assert all(not m.releases for m in result)


def test_diff_releases_keeps_the_list_contract() -> None:
    """The availability-free entry point still returns a plain list ([] when unobserved)."""
    assert _diff_releases({"releases": ["2.4"]}, _jira([]), {"2.4"}) == [
        {"action": "add", "release": "2.4"}
    ]
    assert _diff_releases({"releases": ["2.4"]}, {}, {"2.4"}) == []
