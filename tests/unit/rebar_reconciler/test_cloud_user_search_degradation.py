"""Cloud→DC user-search degradation — story 2127, item 4.

Cloud's assignee resolution has a ``/user/search`` email→accountId bootstrap
(``_bootstrap_account_id_via_user_search``) that DC's transport does not implement. The
resolver must DEGRADE (fall through to the legacy assignable-search string match), NOT
raise, when the transport exposes none of the ``_USER_SEARCH_METHODS`` aliases — otherwise
a DC (or any user-search-less) transport would break outbound assignee resolution.

These tests pin that degradation as a POSITIVE property (a DC-shaped stub degrades) with a
CONTRAST anchor (a Cloud-shaped stub that DOES expose the method uses it), plus the
mutation-check that flips the degradation assertion.

MUTATION-CHECK (recorded RED/GREEN in the change description): add one
``_USER_SEARCH_METHODS`` alias to the DC-shaped stub → ``_bootstrap_account_id_via_user_search``
returns that accountId instead of ``None`` and ``_resolve_assignee_account_id`` reports
``is_account_id=True`` (path 2, not the string-match path 3) → the degradation assertions
below go RED.
"""

from __future__ import annotations

from rebar_reconciler.outbound_assignee import (
    _USER_SEARCH_METHODS,
    _bootstrap_account_id_via_user_search,
    _resolve_assignee_account_id,
)


class _DCTransportStub:
    """DC-shaped transport: exposes NONE of the Cloud ``_USER_SEARCH_METHODS`` aliases.
    ``validate_assignee_exists`` is the legacy assignable-search string match every
    vendor implements — the path resolution must fall through to."""

    def __init__(self, matched: str | None = "DC User") -> None:
        self._matched = matched
        self.validate_calls: list[tuple[str, str | None]] = []

    def validate_assignee_exists(
        self, assignee: str, *, issue_key: str | None = None, project_key: str | None = None
    ) -> str | None:
        self.validate_calls.append((assignee, issue_key))
        return self._matched


class _CloudTransportStub(_DCTransportStub):
    """Cloud-shaped transport: DOES expose the primary user-search alias."""

    def __init__(self, account_id: str = "acct-cloud-1") -> None:
        super().__init__()
        self._account_id = account_id
        self.user_search_calls: list[str] = []

    def search_user_by_email(self, email: str) -> str:
        self.user_search_calls.append(email)
        return self._account_id


def test_dc_transport_exposes_none_of_the_user_search_aliases() -> None:
    """Guard the premise: the DC-shaped stub must not accidentally satisfy any alias, or
    the degradation tests below would be measuring nothing."""
    stub = _DCTransportStub()
    assert not any(hasattr(stub, name) for name in _USER_SEARCH_METHODS)


def test_user_search_bootstrap_degrades_to_none_when_transport_lacks_the_method() -> None:
    """ABSENCE path: with a transport exposing no user-search alias, the bootstrap returns
    ``None`` (degrade), NOT an exception — the caller then uses the string-match path."""
    stub = _DCTransportStub()
    # An email-shaped assignee so an email is derivable without rebar-core identity.
    result = _bootstrap_account_id_via_user_search(
        "someone@example.com",
        stub,  # type: ignore[arg-type]
    )
    assert result is None


def test_resolver_degrades_to_string_match_on_dc_transport_not_raise() -> None:
    """END-TO-END degradation: on a user-search-less transport, ``_resolve_assignee_account_id``
    falls through to path 3 (``validate_assignee_exists`` string match) and returns a
    non-account-id result WITHOUT raising."""
    stub = _DCTransportStub(matched="DC User")
    acct, authoritative, is_account_id = _resolve_assignee_account_id(
        "someone@example.com",
        "DIG-1",
        stub,  # type: ignore[arg-type]
    )
    assert acct == "DC User"
    assert authoritative is True
    assert is_account_id is False, (
        "resolution used an accountId path on a user-search-less transport — it must "
        "degrade to the assignable-search string match (is_account_id=False)"
    )
    assert stub.validate_calls == [("someone@example.com", "DIG-1")], (
        "the string-match fallback (validate_assignee_exists) was not reached"
    )


def test_cloud_transport_uses_user_search_contrast_anchor() -> None:
    """CONTRAST: a transport that DOES expose the alias resolves via the user-search
    bootstrap (path 2) — proving the degradation above is specifically about the alias
    being ABSENT on DC, not about the resolver never using user-search at all."""
    stub = _CloudTransportStub(account_id="acct-cloud-1")
    result = _bootstrap_account_id_via_user_search(
        "someone@example.com",
        stub,  # type: ignore[arg-type]
    )
    assert result == "acct-cloud-1"
    assert stub.user_search_calls == ["someone@example.com"]

    acct, authoritative, is_account_id = _resolve_assignee_account_id(
        "someone@example.com",
        "DIG-1",
        stub,  # type: ignore[arg-type]
    )
    assert (acct, authoritative, is_account_id) == ("acct-cloud-1", True, True)
    # The user-search path short-circuits BEFORE the string match.
    assert stub.validate_calls == []


def test_bootstrap_returns_none_without_client() -> None:
    """No transport at all → degrade to ``None`` (the no-client guard), never raise."""
    assert (
        _bootstrap_account_id_via_user_search(
            "someone@example.com",
            None,  # type: ignore[arg-type]
        )
        is None
    )
