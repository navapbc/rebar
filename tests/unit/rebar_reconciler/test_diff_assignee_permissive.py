"""RED tests for Gap 4: assignee permissive match (no canonical-key churn).

Historical bug (bug 85a1 / Gap 4): the outbound differ compared a local
``assignee`` string against ``_extract_jira_field(jira, "assignee")``,
which always returned ``displayName``. Local tickets store assignee as
email, displayName, "Test", or empty depending on how they were created
(probe vs ticket-create.sh vs Jira-mirror inbound). The bare-string
comparison fired every pass for any non-displayName local — Phase 6
idempotency churn AND spurious outbound updates.

The fix makes the assignee comparison shape-tolerant: a local string
matches a Jira assignee dict if it equals ANY of {emailAddress,
accountId, displayName}. No production push is changed by this — only
the diff is taught to recognize equivalent identities.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "outbound_differ.py"
)


def _load_differ():
    spec = importlib.util.spec_from_file_location(
        "outbound_differ_assignee_test", DIFFER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["outbound_differ_assignee_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ():
    if not DIFFER_PATH.exists():
        pytest.fail(f"outbound_differ.py not found at {DIFFER_PATH}")
    return _load_differ()


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


def test_local_email_matches_jira_email(differ):
    """Local stores email; Jira returns dict — diff should NOT fire."""
    ticket = _make_ticket("joe@example.com")
    jira = _make_jira_fields(JIRA_USER)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" not in changed, (
        f"local email matches Jira emailAddress — no diff expected; got {changed!r}"
    )


def test_local_displayname_matches_jira_displayname(differ):
    """Local stores displayName; same person — diff should NOT fire."""
    ticket = _make_ticket("Joe Oakhart")
    jira = _make_jira_fields(JIRA_USER)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" not in changed


def test_local_accountid_matches_jira_accountid(differ):
    """Local stores accountId; diff should NOT fire."""
    ticket = _make_ticket("712020:abc-123")
    jira = _make_jira_fields(JIRA_USER)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" not in changed


def test_local_different_user_does_fire(differ):
    """When local truly differs (different user identity), diff should fire."""
    ticket = _make_ticket("alice@example.com")
    jira = _make_jira_fields(JIRA_USER)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" in changed
    assert changed["assignee"] == "alice@example.com"


def test_local_unassigned_matches_jira_null(differ):
    """Both unassigned should match."""
    ticket = _make_ticket("")
    jira = _make_jira_fields(None)  # Jira returns None for unassigned
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" not in changed


def test_local_unassigned_vs_jira_assigned_does_fire(differ):
    """Local says unassign but Jira has someone — diff should fire (clear-assignee mutation)."""
    ticket = _make_ticket("")
    jira = _make_jira_fields(JIRA_USER)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" in changed
    assert changed["assignee"] == ""


def test_jira_assignee_missing_emailaddress_falls_back(differ):
    """Some Jira tenants hide email — displayName fallback must still match."""
    user_no_email = {"accountId": "712020:abc-123", "displayName": "Joe Oakhart"}
    ticket = _make_ticket("Joe Oakhart")
    jira = _make_jira_fields(user_no_email)
    changed = differ._diff_fields(ticket, jira)
    assert "assignee" not in changed
