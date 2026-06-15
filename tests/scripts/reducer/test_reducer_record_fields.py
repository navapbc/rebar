"""Reduced-state record fields for the lifecycle event types replayed by
``replay_events``: REVERT, BRIDGE_ALERT, and VERIFY_COMMANDS.

The reducer's per-event processors copy a fixed set of fields out of the event
onto the compiled record (a revert entry, a bridge-alert entry, the
verify-commands list). These tests reduce a CREATE + the event through
``reduce_ticket`` and assert the **exact record content** — every field name and
value — so a dropped/renamed field, a wrong source key, or a missing dispatch arm
is observable in the projection consumers (show/list/MCP) read, not just "the
list is non-empty". They complement test_reducer_file_impact.py (FILE_IMPACT) and
test_reducer_core.py (CREATE/STATUS/COMMENT).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from _events import _UUID, _UUID2, _UUID3, _write_event  # noqa: E402


def _create(ticket_dir: Path) -> None:
    _write_event(
        ticket_dir,
        timestamp=1700000000,
        uuid=_UUID,
        event_type="CREATE",
        data={"title": "T", "ticket_type": "task", "priority": 2},
    )


# ───────────────────────────── REVERT ────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.scripts
def test_revert_record_carries_all_fields(tmp_path: Path, reducer: ModuleType) -> None:
    """A REVERT event compiles to a revert entry whose every field is sourced from
    the event: its own uuid, the target event uuid/type, the reason, and the
    event's timestamp + author."""
    ticket_dir = tmp_path / "tkt-rev"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000001,
        uuid=_UUID2,
        event_type="REVERT",
        data={
            "target_event_uuid": "tgt-uuid-123",
            "target_event_type": "EDIT",
            "reason": "undo a bad edit",
        },
        author="reverter",
    )

    state = reducer.reduce_ticket(ticket_dir)
    assert state is not None
    assert state["reverts"] == [
        {
            "uuid": _UUID2,
            "target_event_uuid": "tgt-uuid-123",
            "target_event_type": "EDIT",
            "reason": "undo a bad edit",
            "timestamp": 1700000001,
            "author": "reverter",
        }
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_revert_reason_defaults_to_empty_string(tmp_path: Path, reducer: ModuleType) -> None:
    """A REVERT with no ``reason`` records the empty string (not None/missing)."""
    ticket_dir = tmp_path / "tkt-rev2"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000001,
        uuid=_UUID2,
        event_type="REVERT",
        data={"target_event_uuid": "x", "target_event_type": "STATUS"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["reverts"][0]["reason"] == ""


@pytest.mark.unit
@pytest.mark.scripts
def test_revert_of_archived_unarchives_ticket(tmp_path: Path, reducer: ModuleType) -> None:
    """Reverting an ARCHIVED event clears the archived projection: status returns
    to ``open`` and ``archived`` is False (the reducer dispatch + process_revert
    unarchive seam, end-to-end)."""
    ticket_dir = tmp_path / "tkt-rev3"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(ticket_dir, timestamp=1700000001, uuid=_UUID2, event_type="ARCHIVED", data={})

    archived = reducer.reduce_ticket(ticket_dir)
    assert archived["status"] == "archived" and archived["archived"] is True

    _write_event(
        ticket_dir,
        timestamp=1700000002,
        uuid=_UUID3,
        event_type="REVERT",
        data={"target_event_uuid": _UUID2, "target_event_type": "ARCHIVED", "reason": "oops"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["status"] == "open"
    assert state["archived"] is False


# ───────────────────────────── BRIDGE_ALERT ──────────────────────────────────
@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_alert_record_fields(tmp_path: Path, reducer: ModuleType) -> None:
    """A BRIDGE_ALERT compiles to an alert entry carrying the event uuid, the
    normalized reason, the timestamp, and resolved=False."""
    ticket_dir = tmp_path / "tkt-ba"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000005,
        uuid=_UUID2,
        event_type="BRIDGE_ALERT",
        data={"alert_type": "drift_detected", "detail": "ignored when alert_type present"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["bridge_alerts"] == [
        {
            "uuid": _UUID2,
            "reason": "drift_detected",
            "timestamp": 1700000005,
            "resolved": False,
        }
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_alert_reason_falls_back_to_detail(tmp_path: Path, reducer: ModuleType) -> None:
    """Reason normalization: with no alert_type/reason, the alert's reason is the
    ``detail`` field (the documented fallback order)."""
    ticket_dir = tmp_path / "tkt-ba2"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000005,
        uuid=_UUID2,
        event_type="BRIDGE_ALERT",
        data={"detail": "only-detail-here"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["bridge_alerts"][0]["reason"] == "only-detail-here"


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_alert_reason_prefers_reason_over_detail(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Reason normalization order: with no alert_type, ``reason`` wins over
    ``detail`` (alert_type > reason > detail > "")."""
    ticket_dir = tmp_path / "tkt-ba-reason"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000005,
        uuid=_UUID2,
        event_type="BRIDGE_ALERT",
        data={"reason": "the-reason", "detail": "the-detail"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["bridge_alerts"][0]["reason"] == "the-reason"


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_alert_resolve_with_no_match_appends_resolved(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A resolving BRIDGE_ALERT whose target uuid matches no open alert APPENDS a
    fresh entry (carrying the event uuid/reason/timestamp) marked resolved=True —
    the fallback branch, not a silent drop."""
    ticket_dir = tmp_path / "tkt-ba-nomatch"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000007,
        uuid=_UUID2,
        event_type="BRIDGE_ALERT",
        data={"resolved": True, "resolves_uuid": "no-such-alert", "alert_type": "drift"},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["bridge_alerts"] == [
        {
            "uuid": _UUID2,
            "reason": "drift",
            "timestamp": 1700000007,
            "resolved": True,
        }
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_alert_resolution_marks_existing(tmp_path: Path, reducer: ModuleType) -> None:
    """A resolving BRIDGE_ALERT (resolved + resolves_uuid) flips the matching open
    alert to resolved=True rather than appending a duplicate."""
    ticket_dir = tmp_path / "tkt-ba3"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000005,
        uuid=_UUID2,
        event_type="BRIDGE_ALERT",
        data={"alert_type": "drift"},
    )
    _write_event(
        ticket_dir,
        timestamp=1700000006,
        uuid=_UUID3,
        event_type="BRIDGE_ALERT",
        data={"resolved": True, "resolves_uuid": _UUID2},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert len(state["bridge_alerts"]) == 1
    assert state["bridge_alerts"][0]["uuid"] == _UUID2
    assert state["bridge_alerts"][0]["resolved"] is True


# ───────────────────────────── VERIFY_COMMANDS ───────────────────────────────
@pytest.mark.unit
@pytest.mark.scripts
def test_verify_commands_replace_semantics(tmp_path: Path, reducer: ModuleType) -> None:
    """VERIFY_COMMANDS sets state.verify_commands to the event's list; a second
    event replaces (last-writer-wins), and the value is a list, not the default."""
    ticket_dir = tmp_path / "tkt-vc"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000001,
        uuid=_UUID2,
        event_type="VERIFY_COMMANDS",
        data={"verify_commands": ["pytest -q", "ruff check"]},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["verify_commands"] == ["pytest -q", "ruff check"]

    _write_event(
        ticket_dir,
        timestamp=1700000002,
        uuid=_UUID3,
        event_type="VERIFY_COMMANDS",
        data={"verify_commands": ["make test"]},
    )
    state2 = reducer.reduce_ticket(ticket_dir)
    assert state2["verify_commands"] == ["make test"]


@pytest.mark.unit
@pytest.mark.scripts
def test_verify_commands_null_becomes_empty_list(tmp_path: Path, reducer: ModuleType) -> None:
    """A VERIFY_COMMANDS event with a null payload compiles to an empty list (the
    ``or []`` guard), never None."""
    ticket_dir = tmp_path / "tkt-vc2"
    ticket_dir.mkdir()
    _create(ticket_dir)
    _write_event(
        ticket_dir,
        timestamp=1700000001,
        uuid=_UUID2,
        event_type="VERIFY_COMMANDS",
        data={"verify_commands": None},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["verify_commands"] == []
