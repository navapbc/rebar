"""HELD-OUT pin on transition-by-DESTINATION-STATUS resolution (bug 7f93, epic e369).

A Jira transition's NAME is not its destination STATUS name. Jira's classic workflow offers
``Start Progress`` (-> status ``In Progress``) and ``Done`` (-> status ``Done``), while every
production caller passes a STATUS name: ``LOCAL_STATUS_TO_JIRA["in_progress"] == "In Progress"``,
which that map is documented to hold ("Local status string -> Jira workflow state name").

Matching only on the transition's own name therefore missed, raised ``ValueError``, and got
SOFT-FAILED into ``bridge_alerts`` by ``apply_handlers.record_backstop_failure`` — the pass exited
0 with no traceback and the status never changed. Measured live by the J11 harness
(ticket 5200-e04e-246e-4aae):
``outbound status did not reach DC: fields.status.name is 'To Do'``, alongside the transport's own
``no transition named 'In Progress' is available ... (available: ['Done', 'Start Progress'])``.

THE PARTIAL-FAILURE SHAPE IS WHY THIS NEEDED A UNIT PIN AS WELL AS THE LIVE CELL. ``Done`` is both
a transition name and a status name on that workflow, so ``closed`` synced by coincidence while
``in_progress`` did not. A test that only exercised ``closed`` would have been green throughout.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

# The CLASSIC workflow shape, which is what the Data Center harness actually serves. Note `Done`
# is deliberately both a transition name AND a destination status: that coincidence is what let
# the bug hide, so the fixture reproduces it rather than simplifying it away.
_CLASSIC = [
    {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
    {"id": "31", "name": "Done", "to": {"name": "Done"}},
]


class _FakeClient:
    def __init__(self, transitions: list[dict[str, Any]]) -> None:
        self._transitions = transitions
        self.transitioned: list[tuple[str, str]] = []

    def transitions(self, remote_id: str) -> list[dict[str, Any]]:
        return self._transitions

    def transition_issue(self, remote_id: str, transition_id: str) -> None:
        self.transitioned.append((remote_id, transition_id))


def _transport(transitions: list[dict[str, Any]] | None = None) -> JiraDataCenterTransport:
    client = _FakeClient(_CLASSIC if transitions is None else transitions)
    return JiraDataCenterTransport(client=client, project="DIG")


def test_a_destination_status_name_resolves_to_its_transition() -> None:
    """THE BUG. `"In Progress"` is a STATUS; the transition that reaches it is `Start Progress`.

    Asserts the transition ID actually dispatched, not merely that nothing raised — an
    implementation that resolved the wrong transition would satisfy a raises-nothing assertion
    while moving the issue to the wrong state.
    """
    transport = _transport()
    transport.transition_issue_by_name("DIG-1", "In Progress")
    assert transport._client.transitioned == [("DIG-1", "11")], (
        "the destination status 'In Progress' did not resolve to transition 'Start Progress' "
        "(id 11) — this is bug 7f93, where the status silently never changed"
    )


def test_an_exact_transition_name_still_wins_unchanged() -> None:
    """The fix is ADDITIVE. A caller passing a genuine transition name is unaffected.

    `test_create_get_update_transition_roundtrip` in the live suite drives by transition name and
    must keep passing with no edit, so that behaviour is pinned here too.
    """
    transport = _transport()
    transport.transition_issue_by_name("DIG-1", "Start Progress")
    assert transport._client.transitioned == [("DIG-1", "11")]


def test_the_transition_name_wins_over_a_same_named_destination() -> None:
    """Resolution ORDER, on the coincidence that hid the bug.

    `Done` names a transition AND is a destination status. Exact transition-name match is tried
    first, so this must resolve via the transition — deterministic, not order-of-iteration luck.
    """
    transitions = [
        {"id": "31", "name": "Done", "to": {"name": "Done"}},
        {"id": "41", "name": "Close Issue", "to": {"name": "Done"}},
    ]
    transport = _transport(transitions)
    transport.transition_issue_by_name("DIG-1", "Done")
    assert transport._client.transitioned == [("DIG-1", "31")], (
        "an exact transition-name match must win over a destination-status match"
    )


def test_an_ambiguous_destination_raises_rather_than_guessing() -> None:
    """Two routes to one status must RAISE, not coin-flip.

    Different transitions can reach the same status via different screens or conditions, and they
    are not interchangeable. Silently picking one would be a choice the caller cannot see or
    predict — the failure mode this whole bug is an instance of.
    """
    transitions = [
        {"id": "51", "name": "Resolve", "to": {"name": "Resolved"}},
        {"id": "52", "name": "Auto-Resolve", "to": {"name": "Resolved"}},
    ]
    transport = _transport(transitions)
    with pytest.raises(ValueError) as caught:
        transport.transition_issue_by_name("DIG-1", "Resolved")
    message = str(caught.value)
    assert "AMBIGUOUS" in message
    assert "Resolve" in message and "Auto-Resolve" in message, (
        f"the ambiguity error must name the competing routes so an operator can pick one; "
        f"got {message!r}"
    )
    assert transport._client.transitioned == [], "nothing may be dispatched on an ambiguous match"


def test_an_unresolvable_name_raises_listing_both_spellings() -> None:
    """Still raises — and the message must show BOTH spellings.

    The old message listed only transition names, which is what made the original failure so hard
    to read: it said `'In Progress'` was unavailable while the instance did offer a route to that
    status. Listing `transition -> destination` is what makes the next occurrence self-explaining.
    """
    transport = _transport()
    with pytest.raises(ValueError) as caught:
        transport.transition_issue_by_name("DIG-1", "Nonexistent")
    message = str(caught.value)
    assert "Start Progress" in message and "In Progress" in message, (
        f"the error must list transitions AND their destination statuses; got {message!r}"
    )
    assert transport._client.transitioned == []


def test_a_transition_without_a_declared_destination_is_tolerated() -> None:
    """A payload lacking `to` must not crash the lookup.

    Not hypothetical defensiveness: the transport already treats the transitions payload as
    untyped vendor data elsewhere, and a `to`-less entry must simply not match rather than raise
    an AttributeError from inside the resolver.
    """
    transitions = [
        {"id": "61", "name": "Odd"},
        {"id": "62", "name": "Start Progress", "to": {"name": "In Progress"}},
    ]
    transport = _transport(transitions)
    transport.transition_issue_by_name("DIG-1", "In Progress")
    assert transport._client.transitioned == [("DIG-1", "62")]
