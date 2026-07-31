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

    def add_field_value(self, field: str, value: str) -> None:
        """Mirrors ``jira.resources.Issue.add_field_value(field, value)``.

        Deliberately modelled on the ISSUE resource with its real TWO-argument
        signature: ``jira.JIRA`` has no ``add_field_value`` at all (verified
        against jira 3.10.5). The transport previously called it on the CLIENT
        with three arguments, so ``add_label`` raised AttributeError on every
        invocation — a bug that survived because the method had no test at any
        tier and this fake exposed no such attribute to contradict it. Keeping
        the fake's surface faithful to the real object is what makes it a
        verified fake rather than a rubber stamp.
        """
        current = list(self.raw["fields"].get(field) or [])
        current.append(value)
        self.raw["fields"][field] = current
        setattr(self.fields, field, current)


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
        issues = list(self.issues.values())
        start_at = _k.get("startAt", 0)
        max_results = _k.get("maxResults")
        if max_results is None:
            return issues[start_at:]
        return issues[start_at : start_at + max_results]

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


def test_missing_extra_raises_naming_the_install_command(monkeypatch) -> None:
    """A missing ``[jira-datacenter]`` extra must name the exact install command.

    This is the "clear install-naming error" the story promised, mirroring
    ``rebar.llm.runner._import_pydantic_ai``. It had no test: the message existed
    in ``transport.py`` but nothing pinned it, so a reword could silently leave an
    operator with an ImportError that does not say what to install. A completion
    verification run on ticket 9fd4 flagged exactly that gap.

    ``monkeypatch.setitem`` on ``sys.modules`` is used rather than
    ``setdefault``: binding the key to ``None`` makes ``import jira`` raise
    ImportError, and monkeypatch restores the previous value (including absence)
    at teardown, so this cannot leak into a sibling test — the failure mode
    recorded on ticket 2bc7.
    """
    import sys

    from rebar_reconciler.adapters.jira_datacenter import transport as _t

    monkeypatch.setitem(sys.modules, "jira", None)

    with pytest.raises(ImportError) as caught:
        _t._jira_client_class()

    message = str(caught.value)
    assert "pip install 'nava-rebar[jira-datacenter]'" in message, (
        "the error must name the exact install command an operator can copy; got: " + message
    )


def test_add_label_appends_without_clobbering_existing_labels(transport) -> None:
    """``add_label`` must APPEND, and must go through the Issue resource.

    Regression for a bug the live tier caught on its first real execution: the
    transport called ``self._client.add_field_value(...)``, but ``jira.JIRA``
    has no such attribute — only ``jira.resources.Issue`` does, with a
    two-argument signature. The method could therefore never have worked. This
    pins both halves: the call reaches the issue resource, and the semantics are
    append (a read-modify-write of the whole list would clobber a concurrent
    edit).
    """
    issue = transport._client.issues["DC-1"]
    issue.raw["fields"]["labels"] = ["pre-existing"]

    transport.add_label("DC-1", "rebar-added")

    labels = transport.get_issue("DC-1")["fields"]["labels"]
    assert labels == ["pre-existing", "rebar-added"], (
        f"add_label must append without resetting existing labels; got {labels!r}"
    )
