"""The Data Center transport's public contract (story J6, epic e369).

The DC transport wraps ``pycontribs/jira`` behind the opt-in ``[jira-datacenter]``
extra. Its defining property is that the library's object model is **unwrapped at
this boundary**: every method returns the raw payload shapes rebar's existing
mappers already consume, so nothing downstream ever learns about ``jira.Issue``.

That property is asserted through ``tests/_jira_shape_contract.py`` — the repo's
existing verified-fake honesty contract, already holding the hermetic
``FakeAcliClient`` and the real ``AcliClient`` to ONE definition of these shapes.
Reusing it here tests the actual claim (DC returns the SAME shapes) and makes
drift impossible to hide: a DC-specific copy of these assertions could quietly
diverge from the Cloud ones while both suites stayed green — which is precisely
the two-implementations-diverging failure this epic exists to prevent.

The transport takes its underlying client as a CONSTRUCTOR PARAMETER so these
tests can inject a fake: no network, and the opt-in extra need not be installed
to run them. The same shapes are re-asserted against a real Jira 8.17.1 in
``tests/external/live_jira_dc/test_transport.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from _jira_shape_contract import assert_comment_map_shape, assert_search_shape


class _LibIssue:
    """Stands in for ``jira.Issue`` — ATTRIBUTE access, not a mapping.

    Deliberately not dict-like: a transport that forwards the library object
    instead of unwrapping it fails the shape contract rather than passing by
    coincidence.
    """

    def __init__(self, key: str, fields: dict[str, Any]) -> None:
        self.key = key
        self.fields = type("Fields", (), dict(fields))()
        self.raw = {"key": key, "fields": dict(fields)}


class _LibComment:
    def __init__(self, cid: str, body: str) -> None:
        self.id = cid
        self.body = body
        self.raw = {"id": cid, "body": body}


class FakeJiraClient:
    """A minimal, stateful stand-in for ``jira.JIRA``."""

    def __init__(self) -> None:
        self.issues: dict[str, _LibIssue] = {
            "DC-1": _LibIssue("DC-1", {"summary": "existing", "description": "plain text"})
        }
        self._next = 2

    def create_issue(self, **fields: Any) -> _LibIssue:
        key = f"DC-{self._next}"
        self._next += 1
        self.issues[key] = _LibIssue(key, {"summary": fields.get("summary", ""), "description": ""})
        return self.issues[key]

    def issue(self, key: str, **_k: Any) -> _LibIssue:
        return self.issues[key]

    def search_issues(self, _jql: str, **_k: Any) -> list[_LibIssue]:
        return list(self.issues.values())

    def comments(self, key: str) -> list[_LibComment]:
        return [_LibComment("10001", "a comment")] if key in self.issues else []

    def add_comment(self, key: str, body: str) -> _LibComment:
        return _LibComment("10002", body)


@pytest.fixture
def transport() -> Any:
    """The DC transport with a fake client injected.

    Pins the constructor seam: the transport accepts its client so it is testable
    without the extra installed and without a network.
    """
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    return JiraDataCenterTransport(client=FakeJiraClient(), project="DC")


# ---------------------------------------------------------------------------
# Unwrapping — asserted through the SHARED contract, not a DC-only copy
# ---------------------------------------------------------------------------


def test_search_issues_returns_rebar_raw_shape(transport: Any) -> None:
    result = transport.search_issues("project = DC")

    assert_search_shape(result)
    assert all(isinstance(i, dict) for i in result), (
        "search_issues must yield plain dicts, not library objects"
    )


def test_get_issue_returns_a_plain_dict(transport: Any) -> None:
    issue = transport.get_issue("DC-1")

    assert isinstance(issue, dict), f"get_issue must return a dict, got {type(issue)}"
    assert isinstance(issue.get("key"), str) and issue["key"]
    assert isinstance(issue.get("fields"), dict)


def test_create_issue_returns_a_plain_dict_with_the_new_key(transport: Any) -> None:
    created = transport.create_issue({"summary": "new one", "issuetype": "Task"})

    assert isinstance(created, dict)
    assert isinstance(created.get("key"), str) and created["key"]
    # the created issue is really there — a create that returned a shape but
    # persisted nothing would pass a shape-only assertion
    assert transport.get_issue(created["key"])["key"] == created["key"]


def test_comment_map_matches_the_shared_contract(transport: Any) -> None:
    assert_comment_map_shape(transport.get_comment_map("DC"))


def test_description_is_plain_text_not_an_adf_document(transport: Any) -> None:
    """DC speaks REST v2, where descriptions are strings. A dict would mean we are
    on Cloud's v3 + ADF path — the exact difference this epic exists to model."""
    description = transport.get_issue("DC-1")["fields"].get("description")

    assert description is None or isinstance(description, str), (
        f"DC descriptions must be plain text, got {type(description)}"
    )
