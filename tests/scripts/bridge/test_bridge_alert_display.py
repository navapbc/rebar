"""Tests for BRIDGE_ALERT detection in ticket-reducer.py and ticket-show.sh/ticket-list.sh.

BRIDGE_ALERT event format (from ticket dso-7n6c contract):
    {
        "event_type": "BRIDGE_ALERT",
        "ticket_id": str,
        "env_id": str,
        "timestamp": int,
        "uuid": str,
        "data": {
            "alert_type": str,
            "detail": str,
            # Optional for resolution events:
            "resolved": True,
            "resolves_uuid": str,  # UUID of the original alert being resolved
        }
    }

The reducer is expected to accumulate bridge_alerts in state:
    state["bridge_alerts"] = [
        {
            "reason": str,       # data.alert_type or data.detail
            "timestamp": int,
            "uuid": str,
            "resolved": bool,
        },
        ...
    ]

Test: python3 -m pytest tests/scripts/test_bridge_alert_display.py -v
All tests must return FAIL (AssertionError or pytest failure) before implementation.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

import rebar.reducer as ticket_reducer

# ---------------------------------------------------------------------------
# Module under test — in-process package (Tier E E7d). The reducer logic lives
# in rebar.reducer; the bash-era engine shim (_engine/ticket-reducer.py) and the
# ticket-show.sh/ticket-list.sh read shims are being deleted, so we drive the
# reducer via the package and the read commands via the in-process read
# handlers (rebar._engine_support.reads) rather than subprocessing helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the rebar.reducer module (in-process reducer logic)."""
    return ticket_reducer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV_ID = "00000000-0000-4000-8000-000000000001"
_BRIDGE_ENV_ID = "cccccccc-0000-4000-8000-000000000003"
_UUID_CREATE = "aaaaaaaa-0000-4000-8000-000000000001"
_UUID_ALERT = "bbbbbbbb-0000-4000-8000-000000000002"
_UUID_RESOLVE = "cccccccc-0000-4000-8000-000000000004"


def _write_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str,
    event_type: str,
    data: dict,
    env_id: str = _ENV_ID,
    author: str = "Test User",
    ticket_id: str | None = None,
) -> Path:
    """Write a well-formed event JSON file and return its path."""
    filename = f"{timestamp}-{uuid}-{event_type}.json"
    payload: dict = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    if ticket_id is not None:
        payload["ticket_id"] = ticket_id
    path = ticket_dir / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_ticket_dir(tmp_path: Path, ticket_id: str = "tkt-alert-001") -> Path:
    """Create and return a ticket directory with a CREATE event."""
    ticket_dir = tmp_path / ticket_id
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Test ticket"},
        env_id=_ENV_ID,
        author="Alice",
    )
    return ticket_dir


# ---------------------------------------------------------------------------
# Test 1: reducer detects an unresolved BRIDGE_ALERT event
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_detects_unresolved_bridge_alert(tmp_path: Path, reducer: ModuleType) -> None:
    """A ticket with one BRIDGE_ALERT has bridge_alerts list with one unresolved entry."""
    ticket_dir = _make_ticket_dir(tmp_path)

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID_ALERT,
        event_type="BRIDGE_ALERT",
        data={
            "alert_type": "sync_conflict",
            "detail": "Status mismatch between local and Jira",
        },
        env_id=_BRIDGE_ENV_ID,
        ticket_id=ticket_dir.name,
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert "bridge_alerts" in state, (
        "state must contain 'bridge_alerts' key when BRIDGE_ALERT events are present"
    )
    alerts = state["bridge_alerts"]
    assert isinstance(alerts, list), "'bridge_alerts' must be a list"
    assert len(alerts) == 1, f"expected 1 alert, got {len(alerts)}: {alerts}"

    alert = alerts[0]
    assert "reason" in alert, "alert entry must have 'reason'"
    assert "timestamp" in alert, "alert entry must have 'timestamp'"
    assert "uuid" in alert, "alert entry must have 'uuid'"
    assert "resolved" in alert, "alert entry must have 'resolved'"
    assert alert["resolved"] is False, "alert must be unresolved"
    assert alert["uuid"] == _UUID_ALERT


# ---------------------------------------------------------------------------
# Test 2: reducer marks an alert resolved when a resolution event is present
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_alert_resolved_by_resolution_event(tmp_path: Path, reducer: ModuleType) -> None:
    """A BRIDGE_ALERT followed by a resolving BRIDGE_ALERT (resolved=True) marks it resolved."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-alert-002")

    # Original alert
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID_ALERT,
        event_type="BRIDGE_ALERT",
        data={
            "alert_type": "sync_conflict",
            "detail": "Status mismatch between local and Jira",
        },
        env_id=_BRIDGE_ENV_ID,
        ticket_id=ticket_dir.name,
    )

    # Resolution event: references the original alert's UUID
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID_RESOLVE,
        event_type="BRIDGE_ALERT",
        data={
            "alert_type": "sync_conflict",
            "detail": "Status mismatch resolved",
            "resolved": True,
            "resolves_uuid": _UUID_ALERT,
        },
        env_id=_BRIDGE_ENV_ID,
        ticket_id=ticket_dir.name,
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert "bridge_alerts" in state, "state must contain 'bridge_alerts'"
    alerts = state["bridge_alerts"]

    # Either the original alert is marked resolved=True, or it is absent from the list
    original_entries = [a for a in alerts if a.get("uuid") == _UUID_ALERT]
    if original_entries:
        assert original_entries[0]["resolved"] is True, (
            "original alert must be marked resolved=True after a resolution event"
        )
    else:
        # Acceptable: original alert removed; only the resolution event remains (or list is empty)
        unresolved = [a for a in alerts if not a.get("resolved", False)]
        assert len(unresolved) == 0, (
            f"no unresolved alerts expected after resolution, got: {unresolved}"
        )


# ---------------------------------------------------------------------------
# Test 3: reducer returns empty bridge_alerts when no BRIDGE_ALERT events exist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_no_alerts_when_none_present(tmp_path: Path, reducer: ModuleType) -> None:
    """A ticket with only a CREATE event has bridge_alerts == [] or key absent."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-alert-003")

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    # The reducer must always include the 'bridge_alerts' key (even when empty)
    # so consumers can rely on it without defensive get() checks.
    assert "bridge_alerts" in state, (
        "state must always contain 'bridge_alerts' key (empty list when no alerts present)"
    )
    alerts = state["bridge_alerts"]
    assert isinstance(alerts, list), "'bridge_alerts' must be a list"
    assert len(alerts) == 0, (
        f"expected no bridge_alerts for ticket with no BRIDGE_ALERT events, got: {alerts}"
    )


# ---------------------------------------------------------------------------
# Test 4: `show` surfaces a bridge alert indicator when unresolved alerts exist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_ticket_show_outputs_health_warning_when_unresolved_alerts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The in-process `show` read surfaces a bridge alert indicator for tickets
    with unresolved alerts (in the printed state JSON and/or the stderr warning).
    """
    from rebar._engine_support.reads import _cmd_show

    # Build a minimal tickets-tracker directory structure
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    ticket_id = "tkt-alert-004"
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Test ticket with alert"},
        env_id=_ENV_ID,
        author="Alice",
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID_ALERT,
        event_type="BRIDGE_ALERT",
        data={
            "alert_type": "sync_conflict",
            "detail": "Status mismatch between local and Jira",
        },
        env_id=_BRIDGE_ENV_ID,
        ticket_id=ticket_id,
    )

    rc = _cmd_show([ticket_id], str(tracker_dir))
    captured = capsys.readouterr()
    combined_output = captured.out + captured.err

    assert rc == 0, f"show returned {rc}; stderr: {captured.err!r}"
    # The output (stdout JSON or stderr warning) must contain some indication of bridge alerts
    assert any(
        indicator in combined_output
        for indicator in ("BRIDGE_ALERT", "bridge_alert", "bridge_alerts", "⚠")
    ), (
        f"show output must contain bridge alert indicator for unresolved alerts.\n"
        f"stdout: {captured.out!r}\nstderr: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: `list` includes bridge_alerts in output for alerted tickets
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_ticket_list_includes_bridge_alerts_in_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The in-process `list` read surfaces bridge_alerts for tickets with alerts
    (in the printed state JSON and/or the stderr warning)."""
    from rebar._engine_support.reads import _cmd_list

    # Build minimal tracker directory
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    ticket_id = "tkt-alert-005"
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Listed ticket with alert"},
        env_id=_ENV_ID,
        author="Alice",
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID_ALERT,
        event_type="BRIDGE_ALERT",
        data={
            "alert_type": "sync_conflict",
            "detail": "Outbound push failed",
        },
        env_id=_BRIDGE_ENV_ID,
        ticket_id=ticket_id,
    )

    rc = _cmd_list([], str(tracker_dir))
    captured = capsys.readouterr()
    combined_output = captured.out + captured.err

    assert rc == 0, f"list returned {rc}; stderr: {captured.err!r}"
    # The list output must surface bridge_alerts for the ticket
    assert any(
        indicator in combined_output
        for indicator in ("bridge_alerts", "BRIDGE_ALERT", "bridge_alert")
    ), (
        f"list output must include bridge_alerts for tickets with unresolved alerts.\n"
        f"stdout: {captured.out!r}\nstderr: {captured.err!r}"
    )
