"""Behavioral contract for the reducer-maintained plan-review phase."""

from __future__ import annotations

import logging

import pytest

from rebar._commands.compact import _snapshot_strip_keys
from rebar.reducer._processors import process_snapshot, process_status
from rebar.reducer._state import make_initial_state


def _status(target: str, current: str, *, uuid: str = "m", force: bool = False) -> dict:
    return {
        "uuid": uuid,
        "timestamp": 10,
        "env_id": "env",
        "data": {"status": target, "current_status": current, "force": force},
    }


def test_winning_status_transitions_project_review_phase() -> None:
    state = make_initial_state()
    assert state["plan_review_phase"] == "planning"

    enter = _status("in_progress", "open")
    process_status(state, enter, enter["data"], "event")
    assert (state["status"], state["plan_review_phase"]) == ("in_progress", "execution")

    preserve = _status("blocked", "in_progress", uuid="n")
    process_status(state, preserve, preserve["data"], "event")
    assert state["plan_review_phase"] == "execution"

    reopen = _status("open", "blocked", uuid="o", force=True)
    process_status(state, reopen, reopen["data"], "event")
    assert (state["status"], state["plan_review_phase"]) == ("open", "planning")


def test_losing_fork_does_not_change_review_phase() -> None:
    state = make_initial_state()
    enter = _status("in_progress", "open", uuid="aaaa")
    process_status(state, enter, enter["data"], "event")
    losing = _status("open", "blocked", uuid="zzzz")
    process_status(state, losing, losing["data"], "event")
    assert state["status"] == "in_progress"
    assert state["plan_review_phase"] == "execution"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("open", "planning"),
        ("idea", "planning"),
        ("in_progress", "execution"),
        ("closed", "execution"),
    ],
)
def test_legacy_snapshot_bootstraps_phase_without_mutating_events(
    status: str, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    state = make_initial_state()
    with caplog.at_level(logging.INFO, logger="rebar.reducer._processors"):
        process_snapshot(
            state,
            {"compiled_state": {"ticket_id": "1111-2222-3333-4444", "status": status}},
        )
    assert state["plan_review_phase"] == expected
    record = next(r for r in caplog.records if getattr(r, "event", None))
    assert (record.event, record.ticket_id, record.compiled_status, record.phase) == (
        "plan_review_phase_bootstrap",
        "1111-2222-3333-4444",
        status,
        expected,
    )


def test_legacy_snapshot_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        process_snapshot(make_initial_state(), {"compiled_state": {"status": "future-state"}})


def test_phase_bearing_snapshot_preserves_historical_value() -> None:
    state = make_initial_state()
    process_snapshot(
        state,
        {
            "compiled_state": {
                "ticket_id": "1111-2222-3333-4444",
                "status": "closed",
                "plan_review_phase": "planning",
            }
        },
    )
    assert state["plan_review_phase"] == "planning"


def test_compaction_does_not_strip_plan_review_phase() -> None:
    assert "plan_review_phase" not in _snapshot_strip_keys()
