"""HELD-OUT oracle for 264f — the implementation MUST NOT see this file.

Placed under tests/unit/rebar_reconciler/ so the package conftest puts the engine on
sys.path and seeds the flat ``rebar_reconciler`` namespace (the engine package is
shadowed by ``rebar._engine``), letting these modules import directly.

Validates the reconciler integration the happy-path seam cannot: the outbound differ
writes the ``_assignee_is_account_id`` sentinel on an identity-mapping hit;
``AcliClient.update_issue`` skips the fuzzy assignable-search when told the assignee
is already an accountId (and does not leak the flag to the ACLI subprocess); and
``set_reporter`` issues the reporter REST PUT unwrapped via ``_direct_rest_put_raw``.
"""

from __future__ import annotations

import rebar_reconciler.acli as acli
import rebar_reconciler.outbound_differ as differ


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
    return {"fields": {"assignee": assignee_dict}}


def test_diff_fields_sets_sentinel_on_identity_mapping_hit() -> None:
    def resolver(assignee, jira_key):  # 3-tuple: (accountId, authoritative, is_account_id)
        return ("acct-ada", True, True)

    changed = differ._diff_fields(
        _ticket("ada@example.com"), _jira(None), assignee_resolver=resolver, jira_key="REB-1"
    )
    assert changed.get("assignee") == "acct-ada"
    assert changed.get("_assignee_is_account_id") is True


def test_diff_fields_no_sentinel_on_search_resolution() -> None:
    def resolver(assignee, jira_key):
        return ("acct-bob", True, False)  # resolved via search, not identity mapping

    changed = differ._diff_fields(
        _ticket("bob"), _jira(None), assignee_resolver=resolver, jira_key="REB-1"
    )
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
