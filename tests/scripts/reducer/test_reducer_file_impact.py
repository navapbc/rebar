"""FILE_IMPACT event type

Split from the former monolithic tests/scripts/test_ticket_reducer.py along
reducer-concern seams. The module-under-test fixture (`reducer`) lives in
conftest.py; event-writing helpers (`_write_event`, `_UUID*`) in _events.py.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path
from types import ModuleType

import pytest

from _events import _UUID, _UUID2, _UUID3, REPO_ROOT, _write_event



# ---------------------------------------------------------------------------
# Tests: FILE_IMPACT event type (story 2985-f04d, task a0a3-09d7)
# These tests MUST FAIL until _state.py, _processors.py, and the reducer
# are updated to handle the FILE_IMPACT event type.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR_FI = str(REPO_ROOT / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR_FI not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR_FI)

from ticket_reducer._state import make_initial_state as _make_initial_state  # noqa: E402
from ticket_reducer._state import make_error_dict as _make_error_dict  # noqa: E402


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_event_compiles_to_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given a CREATE + FILE_IMPACT event, state['file_impact'] equals the list.

    Without the fix: _processors.py has no process_file_impact() handler, so the
    FILE_IMPACT event is silently ignored and state['file_impact'] is either
    absent or [] rather than the expected list.
    """
    ticket_dir = tmp_path / "tkt-fi-001"
    ticket_dir.mkdir()

    ts = 1700000000
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=_UUID,
        event_type="CREATE",
        data={"title": "Test", "ticket_type": "task", "author": "test", "priority": 2},
    )
    _write_event(
        ticket_dir,
        timestamp=ts + 1,
        uuid=_UUID2,
        event_type="FILE_IMPACT",
        data={"file_impact": [{"path": "src/foo.py", "reason": "modified"}]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state for CREATE + FILE_IMPACT"
    assert "file_impact" in state, (
        "state must contain 'file_impact' key after FILE_IMPACT event"
    )
    assert state["file_impact"] == [{"path": "src/foo.py", "reason": "modified"}], (
        f"state['file_impact'] must equal the list from the event; "
        f"got {state.get('file_impact')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_latest_wins_semantics(tmp_path: Path, reducer: ModuleType) -> None:
    """Given two FILE_IMPACT events, state reflects the second (latest) list.

    Without the fix: FILE_IMPACT events are not processed yet; even if partially handled,
    last-write-wins semantics are not yet implemented.
    """
    ticket_dir = tmp_path / "tkt-fi-002"
    ticket_dir.mkdir()

    ts = 1700000010
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=_UUID,
        event_type="CREATE",
        data={"title": "Test", "ticket_type": "task", "author": "test", "priority": 2},
    )
    _write_event(
        ticket_dir,
        timestamp=ts + 1,
        uuid=_UUID2,
        event_type="FILE_IMPACT",
        data={"file_impact": [{"path": "old.py", "reason": "old"}]},
    )
    _write_event(
        ticket_dir,
        timestamp=ts + 2,
        uuid=_UUID3,
        event_type="FILE_IMPACT",
        data={"file_impact": [{"path": "new.py", "reason": "new"}]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "file_impact" in state, "state must contain 'file_impact' key"
    assert state["file_impact"] == [{"path": "new.py", "reason": "new"}], (
        f"Latest FILE_IMPACT event must win (last-write-wins); "
        f"got {state.get('file_impact')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_missing_key_returns_empty_list(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given FILE_IMPACT event with no file_impact key, state['file_impact'] == [].

    Without the fix: FILE_IMPACT events not yet processed; state key is absent.
    """
    ticket_dir = tmp_path / "tkt-fi-003"
    ticket_dir.mkdir()

    ts = 1700000020
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=_UUID,
        event_type="CREATE",
        data={"title": "Test", "ticket_type": "task", "author": "test", "priority": 2},
    )
    _write_event(
        ticket_dir,
        timestamp=ts + 1,
        uuid=_UUID2,
        event_type="FILE_IMPACT",
        data={},  # no file_impact key
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "file_impact" in state, "state must contain 'file_impact' key"
    assert state["file_impact"] == [], (
        f"Missing file_impact key in event data must yield []; "
        f"got {state.get('file_impact')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_null_value_returns_empty_list(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given FILE_IMPACT event with file_impact: null, state['file_impact'] == [].

    Without the fix: FILE_IMPACT events not yet processed; state key is absent.
    """
    ticket_dir = tmp_path / "tkt-fi-004"
    ticket_dir.mkdir()

    ts = 1700000030
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=_UUID,
        event_type="CREATE",
        data={"title": "Test", "ticket_type": "task", "author": "test", "priority": 2},
    )
    _write_event(
        ticket_dir,
        timestamp=ts + 1,
        uuid=_UUID2,
        event_type="FILE_IMPACT",
        data={"file_impact": None},  # JSON null
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "file_impact" in state, "state must contain 'file_impact' key"
    assert state["file_impact"] == [], (
        f"Null file_impact value in event data must yield []; "
        f"got {state.get('file_impact')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_field_in_initial_state() -> None:
    """make_initial_state() returns dict with 'file_impact' key == [].

    Without the fix: make_initial_state() does not yet include the 'file_impact' key.
    """
    state = _make_initial_state()
    assert "file_impact" in state, "make_initial_state() must include 'file_impact' key"
    assert state["file_impact"] == [], (
        f"make_initial_state() must set file_impact=[], got {state.get('file_impact')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_field_in_error_dict() -> None:
    """make_error_dict() returns dict with 'file_impact' key == [].

    Without the fix: make_error_dict() does not yet include the 'file_impact' key.
    """
    err = _make_error_dict("tkt-999", "error", "test error")
    assert "file_impact" in err, "make_error_dict() must include 'file_impact' key"
    assert err["file_impact"] == [], (
        f"make_error_dict() must set file_impact=[], got {err.get('file_impact')!r}"
    )
