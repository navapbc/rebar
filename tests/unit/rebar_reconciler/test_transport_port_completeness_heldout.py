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


#: The shape a Cloud accountId takes. DC has no such thing, so a DC return that looks
#: like this is a mis-identified user, not a username.
_ACCOUNT_ID_SHAPED = "5b10a2844c20165700ede21g"


def test_validate_assignee_exists_returns_the_dc_username(monkeypatch) -> None:
    """HELD-OUT edge. `outbound_assignee.py:108-109` does `acct = client.validate_...` then
    `return (acct or None, True, False)` — the value flows on AS the resolved identity.
    Cloud returns an accountId; DC has none, so DC must return the USERNAME. Returning
    `True`, or an accountId-shaped value, corrupts the identity.

    Presence is NOT the contract here — the parametrized member-set test above already
    asserts `hasattr`, so a `hasattr` check adds nothing. This one CALLS the member and
    pins the returned VALUE, which is the part that flows on as the identity.
    """
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    class _MatchingClient:
        """Exact match on `name`, with an accountId also present on the record —
        so a transport that reached for the Cloud-shaped field would be visible."""

        def search_users(self, user=None, maxResults=50, **kw):
            return [
                {"name": "dcuser-other", "emailAddress": "o@x", "displayName": "Other"},
                {
                    "name": "dcuser",
                    "emailAddress": "d@x",
                    "displayName": "DC User",
                    "accountId": _ACCOUNT_ID_SHAPED,
                },
            ]

    transport = JiraDataCenterTransport.__new__(JiraDataCenterTransport)
    transport._client = _MatchingClient()  # type: ignore[attr-defined]

    resolved = transport.validate_assignee_exists("dcuser", project_key="DC")

    assert resolved == "dcuser", (
        f"DC must resolve to the USERNAME that flows on as the identity; got {resolved!r}"
    )
    assert isinstance(resolved, str) and not isinstance(resolved, bool), (
        f"a bool return is truthy, so `acct or None` keeps it and the identity becomes "
        f"`True`; got {resolved!r} of type {type(resolved).__name__}"
    )
    assert resolved != _ACCOUNT_ID_SHAPED, (
        "DC returned the Cloud-shaped accountId from the user record — DC identities are "
        "minted under `name`, so this would mis-identify every DC assignee"
    )


def test_cloud_validate_assignee_exists_returns_the_account_id_not_the_username() -> None:
    """The OTHER half of the same contract, and it is deliberately DIFFERENT: Cloud's
    resolved identity is the `accountId`, not the display name or email. Asserting only
    the DC half would leave the divergence itself untested — and the two implementations
    are the pair most likely to be "harmonised" by mistake."""
    from rebar_reconciler.adapters.jira.acli import AcliClient

    client = AcliClient.__new__(AcliClient)
    client._direct_rest_get = lambda _path: [  # type: ignore[method-assign]
        {
            "accountId": _ACCOUNT_ID_SHAPED,
            "emailAddress": "d@x",
            "displayName": "DC User",
        }
    ]

    resolved = client.validate_assignee_exists("d@x", project_key="CLOUD")

    assert resolved == _ACCOUNT_ID_SHAPED, (
        f"Cloud must resolve to the accountId — the caller forwards this value to ACLI as "
        f"the assignee identity, and an email or display name there is ambiguous; got "
        f"{resolved!r}"
    )
    assert isinstance(resolved, str) and not isinstance(resolved, bool), (
        f"a bool return is truthy and survives `acct or None`; got {resolved!r}"
    )


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


# ---------------------------------------------------------------------------
# 5. The construction-time guard — enforcement, not just declaration
# ---------------------------------------------------------------------------


class _StubTransport:
    """A transport carrying every required member, used as the conforming baseline."""

    def __init__(self) -> None:
        for member in _REQUIRED_TRANSPORT_MEMBERS_LIVE():
            setattr(self, member, lambda *a, **k: None)


def _REQUIRED_TRANSPORT_MEMBERS_LIVE():
    from rebar_reconciler._backend import _REQUIRED_TRANSPORT_MEMBERS

    return _REQUIRED_TRANSPORT_MEMBERS


def test_the_conformance_guard_rejects_a_transport_missing_a_member() -> None:
    """HELD-OUT edge. Declaring members on the port only helps if something CHECKS at
    the moment a backend is assembled. The failure must name the missing member."""
    from rebar_reconciler._backend import BackendEnvError, assert_transport_conforms

    complete = _StubTransport()
    assert_transport_conforms(complete, vendor="test")  # conforming: must not raise

    delattr(complete, "set_entity_property")
    with pytest.raises(BackendEnvError) as excinfo:
        assert_transport_conforms(complete, vendor="test")
    assert "set_entity_property" in str(excinfo.value), (
        "the guard fired but did not name the missing member, so the operator learns "
        "nothing actionable from it"
    )


def test_the_dc_factory_asserts_conformance_at_construction(monkeypatch) -> None:
    """HELD-OUT edge, and the ACTUAL criterion: the guard must run where a backend is
    BUILT. A guard that exists but is never called from the factory is inert — that is
    the exact shape of the original defect, where the port described members nothing
    enforced. Proven by making the factory build an INCOMPLETE transport and asserting
    construction fails BEFORE a JiraDataCenterBackend is returned."""
    from rebar_reconciler import _backend as backend_mod
    from rebar_reconciler.adapters.jira_datacenter import backend as dc_backend
    from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings
    from rebar_reconciler.adapters.jira_datacenter import transport as dc_transport

    class _Incomplete:
        def __init__(self, **kw) -> None:
            for member in _REQUIRED_TRANSPORT_MEMBERS_LIVE():
                if member != "set_entity_property":  # the member that crashed the live pass
                    setattr(self, member, lambda *a, **k: None)

    monkeypatch.setattr(
        dc_settings,
        "resolve_jira_datacenter_settings",
        lambda: type("_S", (), {"project": "DC", "resolved_statuses": frozenset({"Done"})})(),
    )
    monkeypatch.setattr(dc_transport, "build_client_from_settings", lambda s: object())
    monkeypatch.setattr(dc_transport, "JiraDataCenterTransport", _Incomplete)

    with pytest.raises(backend_mod.BackendEnvError) as excinfo:
        dc_backend._build_jira_datacenter_backend(None)
    assert "set_entity_property" in str(excinfo.value)


def test_the_cloud_factory_also_asserts_conformance(monkeypatch) -> None:
    """HELD-OUT edge. The port states what the CORE requires, so it binds every vendor.
    Guarding only the new backend would leave Cloud free to regress silently."""
    from rebar_reconciler import _backend as backend_mod
    from rebar_reconciler.adapters.jira import acli, acli_subprocess
    from rebar_reconciler.adapters.jira import backend as cloud_backend

    class _Incomplete:
        def __init__(self, **kw) -> None:
            for member in _REQUIRED_TRANSPORT_MEMBERS_LIVE():
                if member != "set_reporter":
                    setattr(self, member, lambda *a, **k: None)

    monkeypatch.setattr(
        acli_subprocess,
        "resolve_jira_settings",
        lambda project_default=None: type(
            "_S", (), {"url": "u", "user": "x@example.com", "api_token": "t", "project": "P"}
        )(),
    )
    monkeypatch.setattr(acli, "AcliClient", _Incomplete)

    with pytest.raises(backend_mod.BackendEnvError) as excinfo:
        cloud_backend._build_jira_backend(None)
    assert "set_reporter" in str(excinfo.value)


def test_the_real_transports_both_satisfy_the_guard() -> None:
    """TEETH for the two tests above: a guard that rejected the REAL transports would
    make every backend unbuildable, so this pins that the guard is correct as well as
    present. If this fails, the shipped product cannot construct a backend at all."""
    from rebar_reconciler._backend import assert_transport_conforms
    from rebar_reconciler.adapters.jira.acli import AcliClient
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    for cls, vendor in ((AcliClient, "jira"), (JiraDataCenterTransport, "jira-datacenter")):
        assert_transport_conforms(cls, vendor=vendor)


# ---------------------------------------------------------------------------
# 6. Observability, reporter idempotence, and the property/assignee contracts
# ---------------------------------------------------------------------------


def test_a_failing_member_emits_a_warning_naming_member_and_remote_id(caplog) -> None:
    """HELD-OUT edge. The logger EXISTING is not the criterion — the record being EMITTED
    is. Seven of the twelve members are invoked from sites that swallow `Exception`, so
    this WARNING is the only signal those paths ever produce. An observability
    requirement with no test asserting emission is exactly the "absence of errors is not
    evidence" trap."""
    from rebar_reconciler.adapters.jira_datacenter import transport as dc_transport

    def _boom():
        raise RuntimeError("the remote said no")

    with caplog.at_level(logging.WARNING, logger=dc_transport.logger.name):
        with pytest.raises(RuntimeError):
            dc_transport._call_logged("set_entity_property", "DC-42", _boom)

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "the member failed and NOTHING was logged — the path is silent again"
    message = records[-1].getMessage()
    assert "set_entity_property" in message, f"the record does not name the member: {message!r}"
    assert "DC-42" in message, f"the record does not name the remote id: {message!r}"


def test_set_entity_property_passes_key_and_value_through_intact() -> None:
    """HELD-OUT edge. A wrong shape breaks correlation WITHOUT raising, so "it did not
    error" is not evidence here. Assert the property key and the value reach the REST
    layer verbatim — a mangled key silently orphans the local_id correlation that
    keyless-pending recovery depends on."""
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    seen: list[tuple] = []

    class _RecordingClient:
        def add_issue_property(self, remote_id, key, value):
            seen.append((remote_id, key, value))

    transport = JiraDataCenterTransport.__new__(JiraDataCenterTransport)
    transport._client = _RecordingClient()  # type: ignore[attr-defined]

    transport.set_entity_property("DC-7", "local_id", "abcd-1234-ef56-7890")

    assert seen == [("DC-7", "local_id", "abcd-1234-ef56-7890")], (
        f"the property did not reach the REST layer intact: {seen!r}"
    )


def test_validate_assignee_exists_raises_assignee_not_found_on_a_definitive_miss() -> None:
    """HELD-OUT edge, load-bearing. `outbound_assignee` branches on
    `type(exc).__name__ == "AssigneeNotFoundError"`. Any OTHER type — a
    NotImplementedError above all — silently downgrades EVERY DC assignee resolution to
    the non-authoritative string-match fallback, permanently. So the error TYPE is the
    contract, not merely that it raises."""
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        AssigneeNotFoundError,
        JiraDataCenterTransport,
    )

    class _NoMatchClient:
        def search_users(self, user=None, maxResults=50, **kw):
            # A substring/relevance hit that is NOT an exact match — DC's search is
            # substring-based, so returning this as the assignee would mis-assign.
            return [{"name": "dcuser-other", "emailAddress": "o@x", "displayName": "Other"}]

    transport = JiraDataCenterTransport.__new__(JiraDataCenterTransport)
    transport._client = _NoMatchClient()  # type: ignore[attr-defined]

    with pytest.raises(AssigneeNotFoundError):
        transport.validate_assignee_exists("dcuser", project_key="DC")


def test_a_successful_dc_reporter_write_leaves_the_next_differ_pass_clean(monkeypatch) -> None:
    """HELD-OUT edge — IDEMPOTENCE, which "the write succeeded" does not establish.

    The DC-specific risk is concrete: `set_reporter` writes `{"reporter": {"name": …}}`
    because DC has no accountId, and the inbound identity seam maps that `name` into
    `account_id`. The loop only closes if the value that comes BACK equals the value
    `_resolve_reporter_account_id` asks for. If it does not, `_diff_reporter` emits a
    reporter mutation on EVERY pass — a write loop that converges never, while each
    individual write reports success."""
    from rebar_reconciler import outbound_field_diff
    from rebar_reconciler.inbound_fields import _identity_of

    # The local reporter resolves to the DC username (DC identities are minted under it).
    monkeypatch.setattr(outbound_field_diff, "_resolve_reporter_account_id", lambda r: "dcuser")

    # What DC hands back for the reporter AFTER a successful set_reporter: a user with a
    # `name` and no accountId. Run it through the REAL identity seam, not a hand-made dict.
    remote_identity = _identity_of({"name": "dcuser", "displayName": "DC User"})
    assert remote_identity["account_id"] == "dcuser", (
        "precondition: the DC identity seam must map `name` into account_id, or the "
        "reporter round-trip cannot close at all"
    )

    changed: dict = {}
    outbound_field_diff._diff_reporter({"reporter": "dcuser"}, remote_identity, changed)
    assert "reporter" not in changed, (
        f"the differ emitted a reporter mutation after a SUCCESSFUL write: {changed!r} — "
        f"the reporter would be rewritten on every pass and the bridge never converges"
    )
