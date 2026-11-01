"""Pin account-id identity propagation across the reconciler (264f).

A mapping hit sets the assignee-is-account-id sentinel. AcliClient skips fuzzy
assignable search without leaking the flag to ACLI. Reporter updates use an
unwrapped raw REST PUT."""

from __future__ import annotations

import rebar.reconciler.adapters.jira.acli as acli
from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.outbound_field_diff import compute_update_fields


def _ticket(assignee: str) -> dict:
    return {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": assignee,
    }


def _jira(assignee_dict) -> dict:
    return {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": assignee_dict,
    }


def _diff(ticket: dict, jira: dict, *, assignee_resolver, jira_key: str) -> dict:
    """Run the live outbound field diff with the Jira backend's mappers."""
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        ticket,
        jira,
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
        assignee_resolver=assignee_resolver,
        jira_key=jira_key,
    )


def test_diff_fields_sets_sentinel_on_identity_mapping_hit() -> None:
    def resolver(assignee, jira_key):  # 3-tuple: (accountId, authoritative, is_account_id)
        return ("acct-ada", True, True)

    changed = _diff(
        _ticket("ada@example.com"), _jira(None), assignee_resolver=resolver, jira_key="REB-1"
    )
    assert changed.get("assignee") == "acct-ada"
    assert changed.get("_assignee_is_account_id") is True


def test_diff_fields_no_sentinel_on_search_resolution() -> None:
    def resolver(assignee, jira_key):
        return ("acct-bob", True, False)  # resolved via search, not identity mapping

    changed = _diff(_ticket("bob"), _jira(None), assignee_resolver=resolver, jira_key="REB-1")
    # On the non-identity (search/legacy) path the differ keeps legacy behaviour —
    # the raw local string is emitted (update_issue re-resolves it); the KEY point is
    # that NO fast-path sentinel is set, so update_issue will NOT skip re-resolution.
    assert changed.get("assignee") == "bob"
    assert changed.get("_assignee_is_account_id") is not True


def test_update_issue_skips_search_when_account_id(monkeypatch) -> None:
    client = acli.AcliClient(jira_url="https://x", user="u", api_token="t")
    calls = {"validate": 0, "submitted": "UNSET"}

    def _spy_validate(self, assignee, *, issue_key=None, project_key=None):
        calls["validate"] += 1
        return "SHOULD-NOT-BE-CALLED"

    def _fake_module_update_issue(jira_key, *, acli_cmd=None, **kwargs):
        calls["submitted"] = kwargs.get("assignee")
        assert "assignee_is_account_id" not in kwargs, "flag leaked to ACLI subprocess"
        return None

    monkeypatch.setattr(acli.AcliClient, "validate_assignee_exists", _spy_validate)
    monkeypatch.setattr(acli, "update_issue", _fake_module_update_issue)

    client.update_issue("REB-1", assignee="acct-ada", assignee_is_account_id=True)
    assert calls["validate"] == 0, "must NOT re-resolve an already-resolved accountId"
    assert calls["submitted"] == "acct-ada", "the accountId is submitted directly"


def test_set_reporter_uses_raw_put(monkeypatch) -> None:
    client = acli.AcliClient(jira_url="https://x", user="u", api_token="t")
    captured = {}

    def _spy_put_raw(self, path, body):
        captured["path"] = path
        captured["body"] = body

    monkeypatch.setattr(acli.AcliClient, "_direct_rest_put_raw", _spy_put_raw)

    client.set_reporter("REB-9", "acct-carol")
    assert captured["path"] == "/rest/api/3/issue/REB-9"
    assert captured["body"] == {"fields": {"reporter": {"accountId": "acct-carol"}}}
