"""RED tests for Gap 4: assignee permissive match (no canonical-key churn).

Historical bug (bug 85a1 / Gap 4): the outbound differ compared a local
``assignee`` string against the Jira assignee's ``displayName`` only. Local
tickets store assignee as email, displayName, "Test", or empty depending on
how they were created (probe vs ticket-create.sh vs Jira-mirror inbound). The bare-string
comparison fired every pass for any non-displayName local — Phase 6
idempotency churn AND spurious outbound updates.

The fix makes the assignee comparison shape-tolerant: a local string
matches a Jira assignee dict if it equals ANY of {emailAddress,
accountId, displayName}. No production push is changed by this — only
the diff is taught to recognize equivalent identities.
"""

from __future__ import annotations

from typing import Any

from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


def _diff(ticket: dict, jira: dict) -> dict[str, Any]:
    """Run the live outbound field diff with the Jira backend's mappers."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket, jira, inbound_mapper=backend.inbound, outbound_mapper=backend.outbound
    )


def _make_ticket(assignee):
    return {
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": assignee,
    }


def _make_jira_fields(assignee_dict):
    return {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": assignee_dict,
    }


JIRA_USER = {
    "accountId": "712020:abc-123",
    "displayName": "Joe Oakhart",
    "emailAddress": "joe@example.com",
}


def test_local_email_matches_jira_email() -> None:
    """Local stores email; Jira returns dict — diff should NOT fire."""
    ticket = _make_ticket("joe@example.com")
    jira = _make_jira_fields(JIRA_USER)
    changed = _diff(ticket, jira)
    assert "assignee" not in changed, (
        f"local email matches Jira emailAddress — no diff expected; got {changed!r}"
    )


def test_local_displayname_matches_jira_displayname() -> None:
    """Local stores displayName; same person — diff should NOT fire."""
    ticket = _make_ticket("Joe Oakhart")
    jira = _make_jira_fields(JIRA_USER)
    changed = _diff(ticket, jira)
    assert "assignee" not in changed


def test_local_accountid_matches_jira_accountid() -> None:
    """Local stores accountId; diff should NOT fire."""
    ticket = _make_ticket("712020:abc-123")
    jira = _make_jira_fields(JIRA_USER)
    changed = _diff(ticket, jira)
    assert "assignee" not in changed


def test_local_different_user_does_fire() -> None:
    """When local truly differs (different user identity), diff should fire."""
    ticket = _make_ticket("alice@example.com")
    jira = _make_jira_fields(JIRA_USER)
    changed = _diff(ticket, jira)
    assert "assignee" in changed
    assert changed["assignee"] == "alice@example.com"


def test_local_unassigned_matches_jira_null() -> None:
    """Both unassigned should match."""
    ticket = _make_ticket("")
    jira = _make_jira_fields(None)  # Jira returns None for unassigned
    changed = _diff(ticket, jira)
    assert "assignee" not in changed


def test_local_unassigned_vs_jira_assigned_does_fire() -> None:
    """Local says unassign but Jira has someone — diff should fire (clear-assignee mutation)."""
    ticket = _make_ticket("")
    jira = _make_jira_fields(JIRA_USER)
    changed = _diff(ticket, jira)
    assert "assignee" in changed
    assert changed["assignee"] == ""


def test_jira_assignee_missing_emailaddress_falls_back() -> None:
    """Some Jira tenants hide email — displayName fallback must still match."""
    user_no_email = {"accountId": "712020:abc-123", "displayName": "Joe Oakhart"}
    ticket = _make_ticket("Joe Oakhart")
    jira = _make_jira_fields(user_no_email)
    changed = _diff(ticket, jira)
    assert "assignee" not in changed
