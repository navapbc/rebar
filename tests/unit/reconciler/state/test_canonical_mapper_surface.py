"""The Jira InboundMapper exposes exact canonical identity and remote-parent keys;
OutboundMapper resolves assignees without requiring vendor shapes in core diffing."""

from __future__ import annotations

import pytest

from rebar.reconciler.adapters.jira.backend import JiraBackend

pytestmark = pytest.mark.unit


def _backend() -> JiraBackend:
    return JiraBackend(transport=object())


_JIRA_FIELDS = {
    "summary": "T",
    "status": {"name": "To Do"},
    "assignee": {"accountId": "acc-1", "displayName": "Jane Doe", "emailAddress": "jane@x.com"},
    "reporter": {"accountId": "acc-2", "displayName": "Rep Orter", "emailAddress": "rep@x.com"},
    "parent": {"key": "DIG-5"},
}


def test_inbound_emits_assignee_identity() -> None:
    out = _backend().inbound.map_remote_to_local(_JIRA_FIELDS)
    assert out["assignee_identity"] == {
        "display": "Jane Doe",
        "email": "jane@x.com",
        "account_id": "acc-1",
    }


def test_inbound_emits_remote_parent_id() -> None:
    out = _backend().inbound.map_remote_to_local(_JIRA_FIELDS)
    assert out["remote_parent_id"] == "DIG-5"


def test_inbound_emits_reporter_identity() -> None:
    out = _backend().inbound.map_remote_to_local(_JIRA_FIELDS)
    assert out["reporter_identity"] == {
        "display": "Rep Orter",
        "email": "rep@x.com",
        "account_id": "acc-2",
    }


def test_inbound_keeps_scalar_assignee_string() -> None:
    """Existing consumers still see the scalar ``assignee`` (additive change)."""
    out = _backend().inbound.map_remote_to_local(_JIRA_FIELDS)
    assert "assignee" in out  # scalar string preserved alongside assignee_identity


def test_resolve_assignee_returns_three_tuple() -> None:
    """OutboundMapper.resolve_assignee(local_value, remote_identity) returns
    (value, authoritative, is_account_id). With an empty local value there is nothing to
    resolve, so it is a non-authoritative result (no live account search needed)."""
    result = _backend().outbound.resolve_assignee("", None)
    assert isinstance(result, tuple) and len(result) == 3
    _value, authoritative, _is_account_id = result
    assert authoritative is False  # empty local -> not authoritative
