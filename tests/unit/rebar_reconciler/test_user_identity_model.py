"""The ``UserIdentityModel`` contract (story J4, epic e369).

User identity is one of the three real Cloud/Data-Center differences: Jira Cloud
identifies users by opaque ``accountId`` (username and userkey were removed for
GDPR), while Data Center identifies them by ``name``.

``adapters/jira_family/identity_model.py`` pins that difference as a contract with
two operations:

* ``resolve(local_value, remote_identity)`` -> ``(value, authoritative, is_account_id)``
  — the 3-state account-resolution fast-path the core diff consults before emitting
  an assignee change (ADR 0035 §(d) canonical-comparison corollary);
* ``to_payload(value)`` -> the deployment's assignee field shape.

Both implementations take their lookup resolver as an EXPLICIT constructor
parameter. Nothing is discovered with ``getattr``: a resolver that silently goes
missing would make every resolution non-authoritative, and a permanently
non-authoritative assignee makes the outbound diff re-emit a change it can never
converge — the churn class epic ``ace2`` exists to fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_family.identity_model import (
    AccountIdIdentity,
    NameIdentity,
)

# --- fakes for the irreducible external boundary (the user lookup) -------------


def _cloud_resolver(mapping: dict[str, str | None]):
    """Cloud lookup: local value -> (accountId | None, authoritative, is_account_id)."""

    def resolve(local_value: str) -> tuple[Any, bool, bool]:
        if local_value not in mapping:
            return (local_value, False, False)  # not authoritative
        account = mapping[local_value]
        return (account, True, account is not None)

    return resolve


def _dc_resolver(mapping: dict[str, str | None]):
    """DC lookup: local value -> (username | None, authoritative, is_account_id=False)."""

    def resolve(local_value: str) -> tuple[Any, bool, bool]:
        if local_value not in mapping:
            return (local_value, False, False)
        return (mapping[local_value], True, False)

    return resolve


# The identity key each deployment compares the resolved value against.
_CLOUD = pytest.param("cloud", id="cloud-accountId")
_DC = pytest.param("dc", id="dc-name")


def _model(kind: str, mapping: dict[str, str | None]):
    if kind == "cloud":
        return AccountIdIdentity(resolver=_cloud_resolver(mapping))
    return NameIdentity(resolver=_dc_resolver(mapping))


def _remote_identity(kind: str, value: str | None) -> dict[str, Any]:
    return {"account_id": value} if kind == "cloud" else {"name": value}


# ---------------------------------------------------------------------------
# The shared 3-state contract — identical for both deployments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [_CLOUD, _DC])
def test_empty_local_value_is_non_authoritative_and_does_no_lookup(kind: str) -> None:
    """Nothing to resolve, so no live user search is issued at all."""
    calls: list[str] = []

    def _tracking(local_value: str) -> tuple[Any, bool, bool]:
        calls.append(local_value)
        return (local_value, True, False)

    model = (
        AccountIdIdentity(resolver=_tracking)
        if kind == "cloud"
        else NameIdentity(resolver=_tracking)
    )
    assert model.resolve("", None) == ("", False, False)
    assert calls == [], "an empty assignee must not trigger a lookup"


@pytest.mark.parametrize("kind", [_CLOUD, _DC])
def test_converged_when_resolved_identity_equals_the_remote_one(kind: str) -> None:
    """The CONVERGED signal: value is None so the caller emits nothing.

    This is what stops the outbound diff re-emitting an assignee that is already
    correct on the remote side.
    """
    resolved = "acct-123" if kind == "cloud" else "jsmith"
    model = _model(kind, {"me@example.com": resolved})

    value, authoritative, _ = model.resolve("me@example.com", _remote_identity(kind, resolved))

    assert value is None
    assert authoritative is True


@pytest.mark.parametrize("kind", [_CLOUD, _DC])
def test_resolving_to_a_different_identity_is_authoritative(kind: str) -> None:
    """A real, successful lookup is authoritative for BOTH deployments — the
    property that lets the diff converge on the next pass."""
    resolved = "acct-123" if kind == "cloud" else "jsmith"
    model = _model(kind, {"me@example.com": resolved})

    value, authoritative, _ = model.resolve(
        "me@example.com", _remote_identity(kind, "somebody-else")
    )

    assert authoritative is True
    assert value is not None


@pytest.mark.parametrize("kind", [_CLOUD, _DC])
def test_unmappable_assignee_yields_desired_unassigned(kind: str) -> None:
    """An authoritative lookup that finds nobody means "should be unassigned"."""
    model = _model(kind, {"ghost@example.com": None})

    assert model.resolve("ghost@example.com", _remote_identity(kind, "acct-1")) == (
        "",
        True,
        False,
    )


@pytest.mark.parametrize("kind", [_CLOUD, _DC])
def test_non_authoritative_lookup_passes_the_local_value_through(kind: str) -> None:
    """When the lookup cannot speak authoritatively the caller keeps its legacy
    permissive string match, so the local value is returned unchanged."""
    model = _model(kind, {})

    value, authoritative, _ = model.resolve("who@example.com", _remote_identity(kind, None))

    assert value == "who@example.com"
    assert authoritative is False


# ---------------------------------------------------------------------------
# Deployment-specific: the identity kind and the wire payload shape
# ---------------------------------------------------------------------------


def test_cloud_resolves_to_an_account_id_and_flags_it() -> None:
    model = AccountIdIdentity(resolver=_cloud_resolver({"me@example.com": "acct-123"}))

    value, authoritative, is_account_id = model.resolve("me@example.com", {"account_id": "other"})

    assert (value, authoritative, is_account_id) == ("acct-123", True, True)


def test_data_center_resolves_to_a_username_and_is_never_an_account_id() -> None:
    """DC has no accountId at all, so the flag is always False — the core diff's
    3-state fast-path keys on it."""
    model = NameIdentity(resolver=_dc_resolver({"me@example.com": "jsmith"}))

    value, authoritative, is_account_id = model.resolve("me@example.com", {"name": "other"})

    assert authoritative is True
    assert is_account_id is False
    assert value == "jsmith"


def test_payload_shapes_differ_per_deployment() -> None:
    assert AccountIdIdentity(resolver=_cloud_resolver({})).to_payload("acct-123") == {
        "accountId": "acct-123"
    }
    assert NameIdentity(resolver=_dc_resolver({})).to_payload("jsmith") == {"name": "jsmith"}
