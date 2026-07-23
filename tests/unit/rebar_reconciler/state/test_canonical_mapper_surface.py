"""Ticket 625b: canonical InboundMapper output + OutboundMapper.resolve_assignee (happy path).

The outbound differ moves to canonical-shape comparison. The Jira InboundMapper's output
gains three EXACTLY-named canonical keys so the core never reads vendor snapshot shapes:

* ``assignee_identity``  = {"display", "email", "account_id"}  (from Jira assignee dict)
* ``reporter_identity``  = same fixed shape                    (from Jira reporter dict)
* ``remote_parent_id``   = bare remote id string               (from Jira parent.key)

The scalar ``assignee`` string stays for existing consumers (additive). The
``OutboundMapper`` re-homes the assignee resolver fast-path as
``resolve_assignee(local_val, jira_key) -> (value, authoritative, is_account_id)``.

Happy-path oracle: the canonical keys and resolver contract on JiraBackend.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.adapters.jira.backend import JiraBackend

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
    value, authoritative, is_account_id = result
    assert authoritative is False  # empty local -> not authoritative
