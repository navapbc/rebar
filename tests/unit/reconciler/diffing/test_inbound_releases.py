"""Inbound Jira fixVersions -> local releases diff (ADR 0124, story lovely-bipartisan-mullet).

Fixtures use the documented Jira Cloud ``Version`` shape
(``{"self", "id", "name", "archived", "released"}``); no production project has a version yet.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rebar.reconciler import inbound_differ
from rebar.reconciler.inbound_collection_diffs import _diff_releases_inbound

pytestmark = pytest.mark.unit


def _version(name: str, vid: str = "10000") -> dict[str, Any]:
    return {
        "self": f"https://example.atlassian.net/rest/api/3/version/{vid}",
        "id": vid,
        "name": name,
        "archived": False,
        "released": False,
    }


def test_set_diff_adds_jira_names_and_removes_local_only_names() -> None:
    jira = {"fixVersions": [_version("2.4", "1"), _version("2.5", "2")]}
    local = {"releases": ["2.4", "old"]}
    assert _diff_releases_inbound(jira, local) == [
        {"action": "add", "release": "2.5"},
        {"action": "remove", "release": "old"},
    ]


def test_equal_sets_yield_nothing() -> None:
    jira = {"fixVersions": [_version("2.4")]}
    assert _diff_releases_inbound(jira, {"releases": ["2.4"]}) == []


@pytest.mark.parametrize(
    "jira_fields",
    [{}, {"fixVersions": None}, {"fixVersions": "2.4"}],
    ids=["absent", "none", "non-list"],
)
def test_unobserved_fix_versions_never_remove_local_releases(jira_fields: dict) -> None:
    assert _diff_releases_inbound(jira_fields, {"releases": ["2.4", "2.5"]}) == []


def test_malformed_entries_are_skipped() -> None:
    jira = {"fixVersions": ["2.4", {"id": "1"}, {"name": 7}, {"name": ""}, _version("2.6")]}
    assert _diff_releases_inbound(jira, {"releases": []}) == [{"action": "add", "release": "2.6"}]


def test_pre_feature_local_ticket_without_releases_key() -> None:
    jira = {"fixVersions": [_version("2.4")]}
    assert _diff_releases_inbound(jira, {}) == [{"action": "add", "release": "2.4"}]


class _Store:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = bindings

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


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


def _jira(names: list[str]) -> dict[str, Any]:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "alice",
        "labels": [],
        "fixVersions": [_version(n, str(i)) for i, n in enumerate(names)],
    }


def test_compute_inbound_mutations_emits_release_records() -> None:
    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot={"PROJ-1": _jira(["2.4"])},
        binding_store=_Store({"PROJ-1": "local-1"}),
        local_tickets_by_id={"local-1": _local([])},
    )
    assert suppressed == 0
    assert len(result) == 1
    assert result[0].releases == [{"action": "add", "release": "2.4"}]
    assert result[0].labels == []


def test_outbound_release_add_suppresses_inbound_remove_and_is_counted() -> None:
    outbound = [
        SimpleNamespace(
            jira_key="PROJ-1",
            fields={},
            labels=[],
            links=[],
            releases=[{"action": "add", "release": "2.5"}],
        )
    ]
    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot={"PROJ-1": _jira(["2.4"])},
        binding_store=_Store({"PROJ-1": "local-1"}),
        local_tickets_by_id={"local-1": _local(["2.4", "2.5"])},
        outbound_mutations=outbound,
    )
    assert suppressed == 1
    assert result == []


def test_outbound_release_remove_suppresses_inbound_add() -> None:
    outbound = [
        SimpleNamespace(
            jira_key="PROJ-1",
            fields={},
            labels=[],
            links=[],
            releases=[{"action": "remove", "release": "2.4"}],
        )
    ]
    result, suppressed = inbound_differ.compute_inbound_mutations(
        jira_snapshot={"PROJ-1": _jira(["2.4", "2.6"])},
        binding_store=_Store({"PROJ-1": "local-1"}),
        local_tickets_by_id={"local-1": _local([])},
        outbound_mutations=outbound,
    )
    assert suppressed == 1
    assert [m.releases for m in result] == [[{"action": "add", "release": "2.6"}]]


def test_suppress_collection_echoes_returns_kept_records_and_drop_count() -> None:
    kept, dropped = inbound_differ._suppress_collection_echoes(
        [
            {"action": "add", "label": "a"},
            {"action": "remove", "label": "b"},
            {"action": "add", "label": "c"},
        ],
        adds={"b"},
        removes={"a"},
        key="label",
    )
    assert kept == [{"action": "add", "label": "c"}]
    assert dropped == 2
