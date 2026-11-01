"""UserIdentityModel must keep Data Center assignment authoritative when resolvable.

Constructor-injected resolution prevents a missing collaborator from making
every assignee non-authoritative and causing repeated outbound changes.
AccountIdIdentity matches Cloud backend decisions state for state."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler._backend import BackendAssigneeNotFoundError
from rebar.reconciler.adapters.jira.backend import JiraBackend
from rebar.reconciler.adapters.jira_family.identity_model import (
    AccountIdIdentity,
    NameIdentity,
)

from .backend_support import FakeTransport

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "reconciler"


def _resolver(mapping: dict[str, tuple[Any, bool, bool]]):
    def resolve(local_value: str) -> tuple[Any, bool, bool]:
        return mapping[local_value]

    return resolve


# ---------------------------------------------------------------------------
# 1. The anti-churn oracle — the regression test for PR #120's defect
# ---------------------------------------------------------------------------


def test_data_center_is_authoritative_for_a_resolvable_assignee() -> None:
    """A resolvable Data Center assignee yields an authoritative identity decision."""
    model = NameIdentity(resolver=_resolver({"me@example.com": ("jsmith", True, False)}))

    _, authoritative, _ = model.resolve("me@example.com", {"name": "someone-else"})

    assert authoritative is True, (
        "DC returned a non-authoritative assignee for a RESOLVABLE user. That makes "
        "the outbound diff re-emit the assignee every pass and never converge "
        "(the ace2 churn class). The resolver must be a real injected lookup, not a "
        "getattr-discovered attribute that is silently absent."
    )


def test_data_center_converges_on_the_second_pass() -> None:
    """A resolved Data Center identity converges on pass two without reassignment."""
    model = NameIdentity(resolver=_resolver({"me@example.com": ("jsmith", True, False)}))

    # pass 1 — remote holds someone else, so a change is emitted
    first_value, first_auth, _ = model.resolve("me@example.com", {"name": "someone-else"})
    assert first_auth is True
    assert first_value == "jsmith"

    # pass 2 — remote now holds what pass 1 emitted; nothing more to say
    second_value, second_auth, _ = model.resolve("me@example.com", {"name": first_value})
    assert second_auth is True
    assert second_value is None, "a converged assignee must emit nothing on the next pass"


# ---------------------------------------------------------------------------
# 2. The failure path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [AccountIdIdentity, NameIdentity])
def test_unresolvable_assignee_raises_the_neutral_error(factory: Any) -> None:
    """A lookup that RAISES must surface as the backend-neutral base type, so core
    ``except`` clauses stay vendor-free — not be swallowed into a silently
    non-authoritative value."""

    def _raising(_local_value: str) -> tuple[Any, bool, bool]:
        raise BackendAssigneeNotFoundError("no such user")

    model = factory(resolver=_raising)

    with pytest.raises(BackendAssigneeNotFoundError):
        model.resolve("ghost@example.com", {})


# ---------------------------------------------------------------------------
# 3. Cloud behavioural parity — asserted against the LIVE backend, not literals
# ---------------------------------------------------------------------------

# (local_value, resolver_result, remote_identity) for every state
# JiraBackend.resolve_assignee's docstring enumerates.
_CLOUD_STATES = [
    pytest.param("", None, None, id="empty-local-value"),
    pytest.param(
        "a@x.com", ("acct-1", False, True), {"account_id": "acct-9"}, id="not-authoritative"
    ),
    pytest.param("a@x.com", ("acct-1", True, True), {"account_id": "acct-1"}, id="converged"),
    pytest.param("a@x.com", (None, True, False), {"account_id": "acct-9"}, id="desired-unassigned"),
    pytest.param(
        "a@x.com", ("acct-1", True, True), {"account_id": "acct-9"}, id="accountid-fastpath"
    ),
    pytest.param(
        "a@x.com", ("a@x.com", True, False), {"account_id": "acct-9"}, id="resolvable-mismatched"
    ),
    pytest.param("a@x.com", ("acct-1", True, True), None, id="no-remote-identity"),
]


@pytest.mark.parametrize(("local_value", "resolver_result", "remote_identity"), _CLOUD_STATES)
def test_account_id_identity_matches_the_live_cloud_backend_state_for_state(
    local_value: str, resolver_result: tuple[Any, bool, bool] | None, remote_identity: Any
) -> None:
    """``AccountIdIdentity`` is Cloud's CURRENT behaviour verbatim.

    Both sides are evaluated live, so this cannot drift the way copied expected
    literals would: if either implementation changes, the equality breaks.
    """
    # The backend holds the outbound resolver, which receives it as a constructor
    # parameter. The identity model must not discover it through getattr.
    backend = JiraBackend(transport=FakeTransport())
    expected = backend.outbound.resolve_assignee(
        local_value,
        remote_identity,
        assignee_resolver=(lambda _lv: resolver_result) if resolver_result is not None else None,
    )

    model = AccountIdIdentity(
        resolver=(lambda _lv: resolver_result) if resolver_result is not None else None
    )
    assert model.resolve(local_value, remote_identity) == expected


def test_cloud_with_no_resolver_keeps_the_permissive_fallback() -> None:
    """The fixture path: no resolver injected at all means the caller keeps its
    legacy permissive string match, NOT an exception."""
    assert AccountIdIdentity(resolver=None).resolve("a@x.com", {"account_id": "acct-9"}) == (
        "a@x.com",
        False,
        False,
    )


# ---------------------------------------------------------------------------
# 4. Structural: the defect class is unwritable, not merely absent
# ---------------------------------------------------------------------------


def test_identity_models_discover_no_collaborator_by_getattr() -> None:
    """Identity models receive collaborators in constructors, never through getattr."""
    path = _REC / "adapters" / "jira_family" / "identity_model.py"
    assert path.is_file(), "adapters/jira_family/identity_model.py does not exist"

    offenders = [
        node.func.id
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
    ]
    assert not offenders, (
        "identity_model.py discovers a collaborator with getattr — inject it as an "
        "explicit constructor parameter instead."
    )


def test_both_models_declare_the_resolver_as_a_constructor_parameter() -> None:
    """Explicit dependency, visible to the type checker and to a reader."""
    import inspect

    for factory in (AccountIdIdentity, NameIdentity):
        params = inspect.signature(factory.__init__).parameters
        assert "resolver" in params, (
            f"{factory.__name__}.__init__ must take an explicit 'resolver' parameter"
        )
