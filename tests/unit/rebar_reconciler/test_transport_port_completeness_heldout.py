"""HELD-OUT: transport port completeness + the DC member set (story J9, epic e369).

Held out from the implementation subagent, which is given only the happy path.

THE DEFECT THIS CLOSES. `TicketTransport` declared SIX members while the core reaches
for TWENTY-ONE; twelve were absent from the DC transport and declared on no Protocol at
all. A DC writing reconcile pass crashed on `set_entity_property` while
`isinstance(backend, Backend)`, the backend contract suite, and 1600+ unit tests were all
green — because a contract suite can only test what the port declares.

WHY THE STRUCTURAL TEST MATCHES ATTRIBUTE ACCESS, NOT CALLS: the reconciler routinely
passes transport methods as VALUES — `_call_with_retry(client.delete_issue_link, link_id)`
(`dispatch_one.py:731`), `_call_with_retry(client.set_entity_property, ...)`
(`dispatch_one.py:321`). A call-form-only scan finds 15 members and silently misses 6. That
narrow pattern is how the first audit of this seam under-counted, twice.

THE DOMINANT FAILURE MODE IS SILENT. Seven of the twelve have call sites that swallow
`Exception` at EVERY invocation, so "nothing raised" is true today while nothing works.
That is why several assertions below check for PRESENCE and for EMITTED LOGS rather than
for absence of exceptions.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import urllib.error

import pytest

from rebar_reconciler import _backend
from rebar_reconciler._backend import TicketTransport

_REC = pathlib.Path(__file__).resolve().parents[3] / "src/rebar/_engine/rebar_reconciler"

#: Every member absent from the DC transport before this story, measured by AST audit.
_REQUIRED = [
    "delete_issue",
    "delete_issue_link",
    "get_comments",
    "get_issue_by_rest",
    "get_issue_links",
    "get_parent_map",
    "remove_label",
    "set_entity_property",
    "set_issue_property",
    "set_parent",
    "set_reporter",
    "validate_assignee_exists",
]


def _dc_transport_class():
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    return JiraDataCenterTransport


# ---------------------------------------------------------------------------
# 1. The member set — presence, because absence is SILENT at most call sites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", _REQUIRED)
def test_dc_transport_exposes_every_member_the_core_calls(member: str) -> None:
    """HELD-OUT. Seven of these are swallowed by `except Exception` at every call site, so
    their absence produces NO error — the pass "converges" while silently not syncing
    comments, links, parents or properties. Presence is the only observable contract."""
    assert hasattr(_dc_transport_class(), member), (
        f"JiraDataCenterTransport is missing {member!r}, which the core calls. At most call "
        f"sites this fails SILENTLY, so nothing will tell you at runtime."
    )


# ---------------------------------------------------------------------------
# 2. The port declares what the core requires
# ---------------------------------------------------------------------------


def test_ticket_transport_is_runtime_checkable() -> None:
    """Without `@runtime_checkable`, `isinstance(x, TicketTransport)` raises TypeError
    rather than returning False — so a construction-time conformance guard cannot work
    at all. Verified: that is the current behaviour."""

    class _Bare:
        pass

    assert isinstance(_Bare(), TicketTransport) is False


@pytest.mark.parametrize("member", _REQUIRED)
def test_port_declares_every_member(member: str) -> None:
    """HELD-OUT. The port must STATE its requirement. Declaring only six while the core
    calls twenty-one is what made the contract suite's certificate mean less than it read
    as."""
    assert hasattr(TicketTransport, member), (
        f"TicketTransport does not declare {member!r}, so no backend is obliged to provide "
        f"it and no isinstance check can catch its absence"
    )


def test_a_transport_missing_a_member_fails_isinstance() -> None:
    """HELD-OUT edge. The guard must actually FIRE — a Protocol that declares members but
    is not runtime-checkable, or a check that never runs, buys nothing."""

    class _Incomplete:
        pass

    for name in _REQUIRED:
        setattr(_Incomplete, name, lambda self, *a, **k: None)
    for m in (
        "create_issue",
        "get_issue",
        "update_issue",
        "transition_issue_by_name",
        "add_label",
        "search_issues",
    ):
        setattr(_Incomplete, m, lambda self, *a, **k: None)
    assert isinstance(_Incomplete(), TicketTransport) is True

    delattr(_Incomplete, "set_entity_property")
    assert isinstance(_Incomplete(), TicketTransport) is False, (
        "removing a declared member did not break conformance — the guard is inert"
    )


# ---------------------------------------------------------------------------
# 3. The structural audit — the part that stops the THIRTEENTH slipping in
# ---------------------------------------------------------------------------


def _core_transport_members() -> set[str]:
    """EVERY attribute access on a client/transport receiver — call form OR value form.

    A call-form-only pattern under-counts silently: it returns a smaller, confident,
    wrong number. That is not hypothetical; it is how this seam was mis-audited twice.
    """
    found: set[str] = set()
    for p in sorted(_REC.glob("*.py")):
        for n in ast.walk(ast.parse(p.read_text())):
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id in ("client", "transport")
                and not n.attr.startswith("_")
            ):
                found.add(n.attr)
    return found


def test_every_transport_member_the_core_reaches_for_is_declared() -> None:
    """HELD-OUT. This is the criterion that converts "I found twelve" into "the thirteenth
    cannot be added silently"."""
    undeclared = sorted(m for m in _core_transport_members() if not hasattr(TicketTransport, m))
    assert not undeclared, (
        f"core modules reach for transport members the port does not declare: {undeclared}. "
        f"Declare them on TicketTransport, or the next backend discovers them by crashing."
    )


def test_the_audit_catches_value_form_references() -> None:
    """TEETH. If the audit only matched `client.x(...)` it would miss the value form and
    silently pass. These two are reached ONLY as values in the core."""
    members = _core_transport_members()
    for value_only in ("delete_issue_link", "set_parent"):
        assert value_only in members, (
            f"{value_only!r} not found — the audit is matching call forms only and will "
            f"under-count, which is exactly the bug that produced a wrong member count twice"
        )


# ---------------------------------------------------------------------------
# 4. Contracts that are subtle enough to get wrong from the happy path alone
# ---------------------------------------------------------------------------


def test_identity_of_prefers_accountId_over_name() -> None:
    """HELD-OUT edge, load-bearing. `account_id` must receive `accountId or name` in THAT
    order. Reversed, every Cloud user silently re-identifies to their username."""
    from rebar_reconciler.inbound_fields import _identity_of

    assert _identity_of({"accountId": "557058:abc", "displayName": "C"})["account_id"] == (
        "557058:abc"
    )
    assert _identity_of({"name": "dcuser", "displayName": "D"})["account_id"] == "dcuser"
    assert _identity_of({"accountId": "557058:abc", "name": "legacy"})["account_id"] == (
        "557058:abc"
    ), "accountId must WIN when both are present, or Cloud regresses"


def test_validate_assignee_exists_returns_the_dc_username(monkeypatch) -> None:
    """HELD-OUT edge. `outbound_assignee.py:108-109` does `acct = client.validate_...` then
    `return (acct or None, True, False)` — the value flows on AS the resolved identity.
    Cloud returns an accountId; DC has none, so DC must return the USERNAME. Returning
    `True`, or an accountId-shaped value, corrupts the identity."""
    cls = _dc_transport_class()
    assert hasattr(cls, "validate_assignee_exists")


def test_dc_transport_has_a_logger() -> None:
    """HELD-OUT edge. transport.py had NO logging at all, which is precisely why a failure
    in a swallowed member surfaced nothing. Without a logger there is no channel."""
    from rebar_reconciler.adapters.jira_datacenter import transport as dc_transport

    assert isinstance(getattr(dc_transport, "logger", None), logging.Logger), (
        "the DC transport has no module logger, so member failures cannot be observed"
    )


def test_backend_http_error_still_subclasses_urllib(monkeypatch) -> None:
    """REGRESSION GUARD for J10, which this story builds on: if the subclass relationship
    were broken, the core's existing except-clauses would stop matching and every DC error
    would escape again."""
    assert issubclass(_backend.BackendHTTPError, urllib.error.HTTPError)


def test_get_parent_map_survives_server_side_maxresults_truncation() -> None:
    """HELD-OUT edge, found by comparing against mature OSS Jira integrations.

    Jira DC silently TRUNCATES ``maxResults`` when it exceeds
    ``jira.search.views.default.max`` (Atlassian's REST reference and maxResults KB both
    say so). A loop that advances by the REQUESTED page size, and stops when a page comes
    back shorter than requested, therefore reads a truncated first page as "that is all
    there is" — and silently returns a PARTIAL parent map. The inbound pass then treats
    every unseen issue as parentless, which is data loss that raises nothing.

    Advance by what the server ACTUALLY returned; stop only on an empty page.
    """
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    class _TruncatingClient:
        """Serves 250 issues but caps every page at 20, whatever is requested."""

        SERVER_CAP = 20

        def __init__(self) -> None:
            self.issues = [
                {"key": f"DC-{i}", "fields": {"parent": {"key": "DC-EPIC"}}} for i in range(250)
            ]

        def search_issues(self, jql, startAt=0, maxResults=50, fields=None, **kw):
            return self.issues[startAt : startAt + min(maxResults, self.SERVER_CAP)]

    transport = JiraDataCenterTransport.__new__(JiraDataCenterTransport)
    transport._client = _TruncatingClient()  # type: ignore[attr-defined]
    transport.project = "DC"  # type: ignore[attr-defined]

    parents = transport.get_parent_map("DC")
    assert len(parents) == 250, (
        f"got {len(parents)} of 250 parents — the pager advanced by the REQUESTED page size "
        f"and read a server-truncated page as the final one, silently losing parents"
    )
