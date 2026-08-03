"""Inbound parent sync is one-way: outbound writes the Epic Link, inbound never read it back
(ticket 9bb9-56a3-e9c0-46e9, discovered from 39c1).

``JiraDataCenterTransport.set_parent`` writes a non-sub-task's parent to the instance-discovered
"Epic Link" custom field (39c1). ``get_parent_map`` — the only inbound parent read — searched
``fields="parent"`` and read ``fields.parent`` alone, so a parent rebar had just written was
invisible on the very next inbound pass. This module pins the closed round trip: ``get_parent_map``
must also discover the "Epic Link" field BY NAME (never hardcoded) and read it for a non-sub-task,
while a sub-task's ``fields.parent`` keeps working and wins on the (degenerate) case both are set.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

_EPIC_FIELD_ID = "customfield_10008"


class _FakeClient:
    """A ``jira.JIRA``-shaped double serving both ``search_issues`` and ``fields()``.

    ``fields_ok`` controls whether ``fields()`` (the instance's field-metadata endpoint, used to
    discover the Epic Link custom field id BY NAME) succeeds at all; ``has_epic_link`` controls
    whether the discovered field list actually names one — the "no Epic Link field" instance
    shape the acceptance criteria call out separately from a hard failure.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]],
        *,
        fields_ok: bool = True,
        has_epic_link: bool = True,
    ) -> None:
        self.issues = issues
        self._fields_ok = fields_ok
        self._has_epic_link = has_epic_link
        self.fields_calls = 0

    def fields(self) -> list[dict[str, Any]]:
        self.fields_calls += 1
        if not self._fields_ok:
            raise RuntimeError("field metadata endpoint unavailable")
        catalog = [{"id": "customfield_10001", "name": "Story Points"}]
        if self._has_epic_link:
            catalog.append({"id": _EPIC_FIELD_ID, "name": "Epic Link"})
        return catalog

    def search_issues(
        self,
        _jql: str,
        startAt: int = 0,
        maxResults: int = 50,
        fields: str | None = None,
        **_k: Any,
    ) -> list[dict[str, Any]]:
        return self.issues[startAt : startAt + maxResults]


class _ExplodingClient:
    """A client whose ``search_issues`` always fails — the degradation contract's trigger."""

    def search_issues(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("DC search endpoint is down")


def _transport(client: Any) -> JiraDataCenterTransport:
    return JiraDataCenterTransport(client=client, project="DC")


def test_epic_parent_is_read_back_from_the_epic_link_field() -> None:
    """THE FIX. A non-sub-task's parent, written as an Epic Link, must round-trip inbound."""
    client = _FakeClient(
        [{"key": "DC-10", "fields": {"issuetype": {"subtask": False}, _EPIC_FIELD_ID: "DC-1"}}]
    )

    parents = _transport(client).get_parent_map("DC")

    assert parents == {"DC-10": "DC-1"}, (
        f"the Epic Link value was not read back as the parent: {parents!r}"
    )


def test_subtask_parent_still_comes_from_fields_parent() -> None:
    """REGRESSION. A sub-task's parent must keep coming from ``fields.parent``."""
    client = _FakeClient([{"key": "DC-11", "fields": {"parent": {"key": "DC-2"}}}])

    parents = _transport(client).get_parent_map("DC")

    assert parents == {"DC-11": "DC-2"}


def test_fields_parent_wins_when_both_shapes_are_present() -> None:
    """PRECEDENCE. When an issue somehow carries both, ``fields.parent`` must win."""
    client = _FakeClient(
        [{"key": "DC-12", "fields": {"parent": {"key": "DC-3"}, _EPIC_FIELD_ID: "DC-9"}}]
    )

    parents = _transport(client).get_parent_map("DC")

    assert parents == {"DC-12": "DC-3"}, (
        f"fields.parent must take precedence over the Epic Link value: {parents!r}"
    )


def test_no_epic_link_field_on_instance_does_not_crash() -> None:
    """An instance with no "Epic Link" field must still return ``fields.parent`` results."""
    client = _FakeClient(
        [{"key": "DC-13", "fields": {"parent": {"key": "DC-4"}}}],
        has_epic_link=False,
    )

    parents = _transport(client).get_parent_map("DC")

    assert parents == {"DC-13": "DC-4"}


def test_field_discovery_failure_hits_the_same_degradation_contract(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``fields()`` failure (metadata endpoint down) must degrade the SAME way a
    ``search_issues`` failure does: ``_resolve_epic_link_field_id`` (shared with
    ``set_parent``, which relies on a raise propagating) never swallows this itself, so
    ``get_parent_map`` must call it from INSIDE its own try so its degradation contract
    still catches it — logging a WARNING and returning ``{}`` rather than raising out."""
    client = _FakeClient([{"key": "DC-14", "fields": {"parent": {"key": "DC-5"}}}], fields_ok=False)

    with caplog.at_level("WARNING"):
        parents = _transport(client).get_parent_map("DC")

    assert parents == {}, f"a fields() failure must degrade to {{}}, got {parents!r}"
    assert any(r.levelname == "WARNING" for r in caplog.records), (
        "get_parent_map must log a WARNING on degradation"
    )


class _Issue:
    """A minimal ``set_parent``-shaped issue double: only what ``_unwrap``/``update`` need."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def update(self, fields: dict[str, Any]) -> None:
        self.raw["fields"].update(fields)


def test_discovery_is_shared_with_set_parent_not_duplicated() -> None:
    """The Epic Link field id must be discovered through ONE shared, cached lookup — two
    independent discovery paths could disagree about the same field (the failure mode
    this ticket's brief calls out). ``set_parent``'s own (outbound, 39c1) lookup
    populates the SAME instance cache ``get_parent_map`` reads, so a prior outbound call
    means inbound makes no second ``fields()`` call at all."""
    client = _FakeClient(
        [{"key": "DC-15", "fields": {"issuetype": {"subtask": False}, _EPIC_FIELD_ID: "DC-1"}}]
    )
    client.issue = lambda _k: _Issue({"key": "DC-5", "fields": {"issuetype": {"subtask": False}}})  # type: ignore[attr-defined]
    transport = _transport(client)

    transport.set_parent("DC-5", "DC-1")
    assert client.fields_calls == 1

    parents = transport.get_parent_map("DC")

    assert client.fields_calls == 1, (
        f"get_parent_map re-discovered the Epic Link field instead of reusing set_parent's "
        f"cached lookup: fields() was called {client.fields_calls} times"
    )
    assert parents == {"DC-15": "DC-1"}


def test_get_parent_map_degradation_contract_is_preserved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``get_parent_map`` must still log a WARNING and return ``{}`` on a real read failure,
    so the inbound pass falls back to parentless rather than aborting."""
    with caplog.at_level("WARNING"):
        parents = _transport(_ExplodingClient()).get_parent_map("DC")

    assert parents == {}
    assert any(r.levelname == "WARNING" for r in caplog.records), (
        "get_parent_map must log a WARNING on degradation"
    )
