"""HELD-OUT characterization of JiraBackend delegation (S2, epic bbf1).

Byte-for-byte pins on the CURRENT outputs of the pure Jira mappers/sanitizers,
asserting that ``JiraBackend``'s role Protocols delegate to them with ZERO behaviour
change. The expected values were captured from the pre-story ``main`` functions
(``outbound_fields._map_local_to_jira_fields``, ``inbound_fields._map_jira_to_local_fields``,
``adapters/jira/jira_fields`` sanitizers). A delegation that drops or rewires a map
fails here.

This file is HELD OUT from the implementation subagent — it is the oracle that proves
the delegation is faithful, not a spec the implementer codes against.
"""

from __future__ import annotations

from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeTransport


def _backend() -> JiraBackend:
    return JiraBackend(transport=FakeTransport())


def test_outbound_map_is_byte_for_byte_jira_fields():
    ticket = {
        "ticket_id": "abc1-2345-6789-0abc",
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "status": "in_progress",
        "assignee": "me@example.com",
    }
    assert _backend().outbound.map_local_to_remote(ticket, None) == {
        "summary": "Add widget",
        "description": "Body text",
        "issuetype": "Story",
        "priority": "High",
        "status": "In Progress",
        "assignee": "me@example.com",
    }


def test_inbound_map_is_byte_for_byte_local_fields():
    jira_fields = {
        "summary": "Add widget",
        "description": "Body text",
        "issuetype": {"name": "Story"},
        "priority": {"name": "High"},
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Me", "emailAddress": "me@example.com"},
    }
    assert _backend().inbound.map_remote_to_local(jira_fields) == {
        "title": "Add widget",
        "description": "Body text",
        "ticket_type": "story",
        "priority": 1,
        "assignee": "Me",
        # ticket 625b: additive canonical identity key emitted when ``assignee`` is
        # present, so the core never reads the raw Jira assignee shape.
        "assignee_identity": {"display": "Me", "email": "me@example.com", "account_id": None},
        "status": "in_progress",
    }


def test_outbound_priority_value_map_pins_all_levels():
    b = _backend()
    expected = {0: "Highest", 1: "High", 2: "Medium", 3: "Low", 4: "Lowest"}
    for local_pri, jira_name in expected.items():
        remote = b.outbound.map_local_to_remote({"title": "t", "priority": local_pri}, None)
        assert remote["priority"] == jira_name


def test_outbound_status_value_map_pins_all_states():
    b = _backend()
    expected = {
        "idea": "IDEA",
        "open": "To Do",
        "in_progress": "In Progress",
        "closed": "Done",
        "blocked": "In Progress",
        "cancelled": "Done",
    }
    for local_status, jira_state in expected.items():
        remote = b.outbound.map_local_to_remote({"title": "t", "status": local_status}, None)
        assert remote["status"] == jira_state


def test_sanitize_summary_truncates_to_254_with_suffix():
    out = _backend().sanitizer.sanitize_summary("x" * 300)
    assert len(out) == 254
    assert out.endswith(" [truncated]")
    assert out.startswith("x" * 100)


def test_sanitize_comment_truncates_to_32767_with_marker():
    out = _backend().sanitizer.sanitize_comment("c" * 40000)
    assert len(out) == 32767
    assert out.endswith(" … [truncated by reconciler]")


def test_sanitize_label_strips_and_returns_clean_token():
    assert _backend().sanitizer.sanitize_label("  rebar-id:foo  ") == "rebar-id:foo"


def test_sanitize_short_values_pass_through_unchanged():
    b = _backend()
    assert b.sanitizer.sanitize_summary("Fine") == "Fine"
    assert b.sanitizer.sanitize_comment("hello") == "hello"
