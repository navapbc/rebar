"""Pin Data Center's empty-assignee vendor contract (bug 751e).

The differ supplies ``assignee=""``, but ``jira==3.10.5`` uses ``None`` for
unassigned. ``-1`` and ``"-1"`` select the automatic project assignee; ``""`` performs
user search and may fail or choose an arbitrary user. The transport must therefore map
empty input to ``None``. The default suite omits the optional Jira package, so the fake
reproduces its passthrough and search fallback. Assertions inspect the resulting assignee
field—absence of an exception cannot detect the guarded silent failure.
"""

from __future__ import annotations

from email.message import Message
from typing import Any

import pytest

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter.transport import (
    AssigneeNotFoundError,
    JiraDataCenterTransport,
)


class _FakeUser:
    """A DC user as ``search_users`` returns it (``name`` is DC's identifier)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.key = name


class _FakeJiraClient:
    """A faithful stand-in for ``pycontribs``' ``JIRA`` on a SELF-HOSTED (DC) instance.

    ``assign_issue`` reimplements ``JIRA.assign_issue`` + ``JIRA._get_user_id`` for
    ``_is_cloud=False``: the three passthrough sentinels go straight into the
    ``{"name": …}`` payload, and anything else is looked up via ``search_users``.
    ``assignee_payloads`` records what was PUT so a test can distinguish "sent the unassign
    payload" from "sent something else that happened not to blow up".
    """

    def __init__(self, *, search_results: list[_FakeUser] | None = None) -> None:
        # Start ASSIGNED: an unassign that is a no-op must be visible as "still alice".
        self.assignee: str | None = "alice"
        self.assignee_payloads: list[dict[str, Any]] = []
        self.edited_fields: list[dict[str, Any]] = []
        self.search_calls: list[Any] = []
        self._search_results = search_results if search_results is not None else []

    # -- pycontribs surface -------------------------------------------------
    def search_users(self, **kwargs: Any) -> list[_FakeUser]:
        self.search_calls.append(kwargs)
        return list(self._search_results)

    def _get_user_id(self, user: Any) -> Any:
        if user in (None, -1, "-1"):
            return user
        users = self.search_users(user=user, maxResults=20)
        if len(users) < 1:
            # pycontribs raises ``JIRAError`` here; by the time it reaches
            # ``JiraDataCenterTransport._assign`` it has already been translated by
            # ``retry._with_connection_retry``, so the fake raises the translated type.
            raise BackendHTTPError(
                "", 400, f"No matching user found for: '{user}'", Message(), None
            )
        matches = [u for u in users if u.name == user]
        return (matches[0] if matches else users[0]).name

    def assign_issue(self, remote_id: str, assignee: Any) -> bool:
        user_id = self._get_user_id(assignee)
        self.assignee_payloads.append({"name": user_id})
        # DC clears the field for a null name and for the "-1" automatic sentinel it
        # picks the project default; only the null case is an unassign.
        self.assignee = None if user_id is None else str(user_id)
        return True

    def issue(self, remote_id: str) -> Any:
        client = self

        class _Issue:
            raw = None

            def __init__(self) -> None:
                self.raw = {
                    "key": remote_id,
                    "fields": {
                        "assignee": (None if client.assignee is None else {"name": client.assignee})
                    },
                }

            def update(self, fields: dict[str, Any]) -> None:
                client.edited_fields.append(dict(fields))

        return _Issue()


def _transport(**kwargs: Any) -> tuple[JiraDataCenterTransport, _FakeJiraClient]:
    client = _FakeJiraClient(**kwargs)
    return JiraDataCenterTransport(client=client, project="DIG"), client


@pytest.mark.parametrize("empty", ["", None])
def test_empty_outbound_assignee_actually_unassigns(empty: Any) -> None:
    """Require an empty desired assignee to leave the resulting field empty."""
    transport, client = _transport()
    assert client.assignee == "alice", "precondition: the issue starts assigned"

    result = transport.update_issue("DIG-1", assignee=empty)

    assert result["fields"]["assignee"] is None, (
        f"assignee={empty!r} must leave the DC issue UNASSIGNED, "
        f"but fields.assignee is {result['fields']['assignee']!r}"
    )
    assert client.assignee is None
    assert client.assignee_payloads == [{"name": None}], (
        "the vendor call must carry pycontribs' unassign sentinel (None -> "
        f'{{"name": null}}), got {client.assignee_payloads!r}'
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_empty_assignee_never_assigns_an_arbitrary_search_hit(blank: str) -> None:
    """The WORSE mode story 5200's J11 cell flagged: a blank user search that MATCHES.

    ``search_users(user="")`` is not guaranteed to come back empty — on a DC instance it can
    return the first N users, and ``_get_user_id`` then picks ``users[0]`` and assigns THAT
    person. An empty assignee must never reach the search at all.
    """
    transport, client = _transport(search_results=[_FakeUser("bob"), _FakeUser("carol")])

    result = transport.update_issue("DIG-1", assignee=blank)

    assert result["fields"]["assignee"] is None, (
        f"assignee={blank!r} must not be resolved to a user; "
        f"fields.assignee is {result['fields']['assignee']!r}"
    )
    assert client.assignee is None
    assert client.search_calls == [], (
        f"an empty assignee must not trigger a user search, got {client.search_calls!r}"
    )


def test_non_empty_assignee_still_assigns() -> None:
    """The normal path is untouched: a real username still lands on the issue."""
    transport, client = _transport(search_results=[_FakeUser("bob")])

    result = transport.update_issue("DIG-1", assignee="bob")

    assert result["fields"]["assignee"] == {"name": "bob"}
    assert client.assignee == "bob"
    assert client.assignee_payloads == [{"name": "bob"}]
    assert client.search_calls == [{"user": "bob", "maxResults": 20}]


def test_unresolvable_non_empty_assignee_still_raises() -> None:
    """A bogus (but non-empty) assignee must keep failing loudly, not become an unassign."""
    transport, client = _transport(search_results=[])

    with pytest.raises(AssigneeNotFoundError):
        transport.update_issue("DIG-1", assignee="nosuchuser")

    assert client.assignee == "alice", "a failed assign must not clear the field"


def test_unassign_co_submitted_with_an_editable_field_does_both() -> None:
    """A mutation that clears the assignee AND edits a field must apply both halves."""
    transport, client = _transport()

    result = transport.update_issue("DIG-1", assignee="", summary="new summary")

    assert client.edited_fields == [{"summary": "new summary"}]
    assert result["fields"]["assignee"] is None
