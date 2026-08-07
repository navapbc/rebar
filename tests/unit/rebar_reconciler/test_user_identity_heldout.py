"""HELD-OUT oracle for the ``UserIdentityModel`` contract (story J4, epic e369).

This file is HELD OUT from the implementation subagent.

Three things it proves that the happy-path spec deliberately does not:

1. **The anti-churn regression oracle.** GitHub PR #120's proposed Data Center
   adapter resolved its assignee through ``getattr(self, "_assignee_resolver", None)``
   on a class where nothing ever set that attribute, so its entire authoritative
   branch was dead code and DC returned ``authoritative=False`` for EVERY assignee.
   That is not cosmetic: a permanently non-authoritative assignee makes the outbound
   diff re-emit a change it can never converge — the churn class epic ``ace2`` exists
   to fix. These tests fail against any ``getattr``-discovered-collaborator design.

2. **Cloud behavioural parity, state for state.** ``AccountIdIdentity`` must
   reproduce the pre-story ``JiraBackend.resolve_assignee`` decision table exactly
   (its docstring enumerates the states). The parity is asserted against the LIVE
   ``JiraBackend`` rather than against copied literals, so the two cannot drift.

3. **The structural guarantee**: no ``getattr``-discovered collaborator exists inside
   the identity models — the property that makes defect (1) unwritable rather than
   merely absent.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler._backend import BackendAssigneeNotFoundError
from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.adapters.jira_family.identity_model import (
    AccountIdIdentity,
    NameIdentity,
)

from .backend_support import FakeTransport

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _resolver(mapping: dict[str, tuple[Any, bool, bool]]):
    def resolve(local_value: str) -> tuple[Any, bool, bool]:
        return mapping[local_value]

    return resolve


# ---------------------------------------------------------------------------
# 1. The anti-churn oracle — the regression test for PR #120's defect
# ---------------------------------------------------------------------------


def test_data_center_is_authoritative_for_a_resolvable_assignee() -> None:
    """THE oracle for this story.

    A DC implementation that discovers its resolver via ``getattr`` on an attribute
    nobody sets returns ``authoritative=False`` here and fails. Only a real, injected
    lookup passes.
    """
    model = NameIdentity(resolver=_resolver({"me@example.com": ("jsmith", True, False)}))

    _, authoritative, _ = model.resolve("me@example.com", {"name": "someone-else"})

    assert authoritative is True, (
        "DC returned a non-authoritative assignee for a RESOLVABLE user. That makes "
        "the outbound diff re-emit the assignee every pass and never converge "
        "(the ace2 churn class). The resolver must be a real injected lookup, not a "
        "getattr-discovered attribute that is silently absent."
    )


def test_data_center_converges_on_the_second_pass() -> None:
    """The end-to-end consequence of being authoritative, asserted as behaviour:
    once the resolved identity matches the remote, the model signals CONVERGED
    (``value is None``) so the caller emits nothing and the churn loop terminates."""
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
    # assignee resolution is the OUTBOUND role's job; the backend facade merely holds it.
    # The resolver reaches it as a DECLARED parameter (ticket 65d7); it used to be set on
    # the mapper as ``backend.outbound._assignee_resolver`` and rediscovered by ``getattr``,
    # the side-channel this oracle's own structural test now forbids. Same six states, same
    # live-vs-live comparison — only how the collaborator arrives has changed.
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
    """The resolver arrives as a declared constructor parameter.

    A ``getattr(self, "_assignee_resolver", None)`` lookup is what let PR #120 ship a
    permanently-dead authoritative branch: the attribute was simply never set and
    nothing failed loudly. Forbidding the pattern here makes that class of bug
    impossible rather than merely fixed once.
    """
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
