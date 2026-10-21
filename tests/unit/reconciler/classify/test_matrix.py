"""Table test of the FULL classify() matrix (epic 3006-e198 foundation).

Every ``(LocalState × ObservedJira × binding-present)`` cell is enumerated,
including the ADR 0028 regression cells (absent-in-window / Done-beyond-cap →
PROBE_GET not RETIRE; transport-error → NOOP). An unmatched cell must RAISE, not
fall through to a silent default.
"""

from __future__ import annotations

import pytest

from ._load import load_classify

c = load_classify()
ObservedJira = c.ObservedJira
DecisionKind = c.DecisionKind
JiraObservation = c.JiraObservation

ACTIVE = {"ticket_id": "loc-1", "status": "in_progress", "archived": False}
ARCHIVED = {"ticket_id": "loc-1", "status": "archived", "archived": True}
DELETED = {"ticket_id": "loc-1", "status": "deleted", "archived": False}
CONFIRMED_BINDING = {"jira_key": "REB-1", "state": "confirmed"}


def _present(status: str = "In Progress", *, retired: bool = False) -> JiraObservation:
    return JiraObservation(
        ObservedJira.PRESENT, key="REB-1", fields={"status": status}, retired=retired
    )


def _obs(state) -> JiraObservation:
    return JiraObservation(state, key="REB-1")


# ── bound cells ──────────────────────────────────────────────────────────────


def test_bound_active_present_is_sync_fields():
    d = c.classify(ACTIVE, _present(), CONFIRMED_BINDING, None)
    assert d.kind is DecisionKind.SYNC_FIELDS
    assert not d.is_acting


@pytest.mark.parametrize("local", [ARCHIVED, DELETED])
def test_bound_terminal_present_live_is_terminal_transition(local):
    d = c.classify(local, _present("To Do"), CONFIRMED_BINDING, None)
    assert d.kind is DecisionKind.TERMINAL_TRANSITION
    assert d.is_acting


@pytest.mark.parametrize("done_status", ["Done", "Cancelled"])
def test_bound_terminal_present_already_done_is_noop(done_status):
    # Idempotent steady state: local terminal + Jira already terminal → nothing.
    d = c.classify(ARCHIVED, _present(done_status), CONFIRMED_BINDING, None)
    assert d.kind is DecisionKind.NOOP


def test_bound_absent_in_window_is_probe_get_regardless_of_local():
    # ADR 0028 §1 — absence is NOT deletion (Done-beyond-cap is alive-but-absent).
    for local in (ACTIVE, ARCHIVED, DELETED):
        d = c.classify(local, _obs(ObservedJira.ABSENT_IN_WINDOW), CONFIRMED_BINDING, None)
        assert d.kind is DecisionKind.PROBE_GET
        assert not d.is_acting


def test_bound_transport_error_is_noop_defer():
    # ADR 0028 §2 — a transport error is not evidence of anything; defer.
    d = c.classify(ACTIVE, _obs(ObservedJira.TRANSPORT_ERROR), CONFIRMED_BINDING, None)
    assert d.kind is DecisionKind.NOOP


def test_bound_confirmed_404_at_grace_is_retire():
    binding = {"jira_key": "REB-1", "state": "confirmed", "absent_404_count": 2}
    d = c.classify(ARCHIVED, _obs(ObservedJira.CONFIRMED_404), binding, None, grace=3)
    assert d.kind is DecisionKind.RETIRE_AFTER_GRACE
    assert d.is_acting
    assert d.payload["absent_404_count"] == 3


def test_bound_confirmed_404_below_grace_keeps_probing():
    binding = {"jira_key": "REB-1", "state": "confirmed", "absent_404_count": 0}
    d = c.classify(ARCHIVED, _obs(ObservedJira.CONFIRMED_404), binding, None, grace=3)
    assert d.kind is DecisionKind.PROBE_GET
    assert not d.is_acting


def test_bound_local_absent_present_is_alert():
    d = c.classify(None, _present(), CONFIRMED_BINDING, None)
    assert d.kind is DecisionKind.ALERT
    assert not d.is_acting


def test_pending_binding_is_noop():
    pending = {"jira_key": None, "state": "pending"}
    d = c.classify(ACTIVE, _present(), pending, None)
    assert d.kind is DecisionKind.NOOP


# ── unbound cells (adoption) ─────────────────────────────────────────────────


def test_unbound_present_not_retired_is_adopt():
    d = c.classify(None, _present("To Do"), None, None)
    assert d.kind is DecisionKind.ADOPT
    assert d.is_acting


def test_unbound_present_retired_is_skip_retired():
    # ADR 0027 §4a — a just-retired key must not be resurrected (delete/re-adopt loop).
    d = c.classify(None, _present("To Do", retired=True), None, None)
    assert d.kind is DecisionKind.SKIP_RETIRED
    assert not d.is_acting


@pytest.mark.parametrize(
    "state",
    [ObservedJira.CONFIRMED_404, ObservedJira.ABSENT_IN_WINDOW, ObservedJira.TRANSPORT_ERROR],
)
def test_unbound_not_present_is_noop(state):
    d = c.classify(None, _obs(state), None, None)
    assert d.kind is DecisionKind.NOOP


# ── coverage: every enum combination is decided (no unmatched cell) ──────────


def test_every_cell_is_decided_never_raises():
    binding_opts = [None, CONFIRMED_BINDING, {"jira_key": "REB-1", "state": "pending"}]
    local_opts = [None, ACTIVE, ARCHIVED, DELETED]
    seen_kinds = set()
    for state in ObservedJira:
        for local in local_opts:
            for binding in binding_opts:
                for retired in (False, True):
                    obs = JiraObservation(
                        state,
                        key="REB-1",
                        fields={"status": "To Do"} if state is ObservedJira.PRESENT else None,
                        retired=retired,
                    )
                    d = c.classify(local, obs, binding, None)
                    assert d.kind in DecisionKind
                    seen_kinds.add(d.kind)
    # The matrix must exercise the full closed decision set at least once.
    assert seen_kinds >= {
        DecisionKind.SYNC_FIELDS,
        DecisionKind.TERMINAL_TRANSITION,
        DecisionKind.PROBE_GET,
        DecisionKind.SKIP_RETIRED,
        DecisionKind.ADOPT,
        DecisionKind.ALERT,
        DecisionKind.NOOP,
    }
