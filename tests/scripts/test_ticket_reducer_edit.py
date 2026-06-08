"""RED tests for EDIT event support in ticket-reducer.py.

These tests are RED — they test functionality that does not yet exist.
All test functions must FAIL until the reducer handles EDIT events.

The reducer is expected to apply EDIT events by merging `data.fields` into
the current ticket state (last-writer-wins for sequential edits).

EDIT event structure:
    {
        "timestamp": <int>,
        "uuid": "<uuid>",
        "event_type": "EDIT",
        "env_id": "<uuid>",
        "author": "<str>",
        "data": {
            "fields": {
                "<field>": <value>,
                ...
            }
        }
    }

Test: python3 -m pytest tests/scripts/test_ticket_reducer_edit.py
All tests must return non-zero until EDIT event handling is implemented.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-reducer.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_reducer", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def reducer() -> ModuleType:
    """Return the ticket-reducer module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-reducer.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_CREATE = "aaaaaaaa-0001-4000-8000-000000000001"
_UUID_EDIT_1 = "bbbbbbbb-0002-4000-8000-000000000002"
_UUID_EDIT_2 = "cccccccc-0003-4000-8000-000000000003"


def _write_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str,
    event_type: str,
    data: dict,
    env_id: str = "00000000-0000-4000-8000-000000000001",
    author: str = "Test User",
) -> Path:
    """Write a well-formed event JSON file and return its path."""
    filename = f"{timestamp}-{uuid}-{event_type}.json"
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path


def _make_ticket_dir(tmp_path: Path, name: str = "tkt-edit") -> Path:
    """Create and return a named ticket directory under tmp_path."""
    d = tmp_path / name
    d.mkdir()
    return d


def _base_create_data(title: str = "Original Title") -> dict:
    return {
        "ticket_type": "task",
        "title": title,
        "parent_id": "",
    }


# ---------------------------------------------------------------------------
# Test 1: reducer applies EDIT event to title
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_edit_event_to_title(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An EDIT event with fields.title overwrites the title from the CREATE event."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-title")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(title="Original"),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"title": "Updated"}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert state["title"] == "Updated", (
        f"EDIT event must update title; got {state.get('title')!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: reducer applies EDIT event to priority
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_edit_event_to_priority(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An EDIT event with fields.priority overwrites the priority field."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-priority")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"priority": 1}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert state["priority"] == 1, (
        f"EDIT event must update priority; got {state.get('priority')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: reducer applies EDIT event to assignee
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_edit_event_to_assignee(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An EDIT event with fields.assignee sets the assignee on the ticket state."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-assignee")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"assignee": "Jane"}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert state["assignee"] == "Jane", (
        f"EDIT event must set assignee; got {state.get('assignee')!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: reducer applies multiple fields in a single EDIT event
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_multiple_fields_in_single_edit(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A single EDIT event can update title, priority, and assignee simultaneously."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-multi")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(title="Old Title"),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"title": "New", "priority": 0, "assignee": "Bob"}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert state["title"] == "New", (
        f"EDIT event must update title; got {state.get('title')!r}"
    )
    assert state["priority"] == 0, (
        f"EDIT event must update priority; got {state.get('priority')!r}"
    )
    assert state["assignee"] == "Bob", (
        f"EDIT event must update assignee; got {state.get('assignee')!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: reducer applies sequential EDIT events — last writer wins
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_sequential_edit_events(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """When two EDIT events update the same field, the later timestamp wins."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-sequential")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"priority": 1}},
    )
    _write_event(
        ticket_dir,
        timestamp=3000,
        uuid=_UUID_EDIT_2,
        event_type="EDIT",
        data={"fields": {"priority": 2}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    assert state["priority"] == 2, (
        f"last EDIT event must win (last-writer-wins); got {state.get('priority')!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: EDIT event does not affect unedited fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_edit_does_not_affect_unedited_fields(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An EDIT event that only touches priority must leave title unchanged."""
    ticket_dir = _make_ticket_dir(tmp_path, "tkt-edit-isolation")

    _write_event(
        ticket_dir,
        timestamp=1000,
        uuid=_UUID_CREATE,
        event_type="CREATE",
        data=_base_create_data(title="Keep me"),
    )
    _write_event(
        ticket_dir,
        timestamp=2000,
        uuid=_UUID_EDIT_1,
        event_type="EDIT",
        data={"fields": {"priority": 1}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict"
    # The EDIT event must be applied (priority updated) — this fails RED if EDIT is ignored
    assert state["priority"] == 1, (
        f"EDIT event must update priority; got {state.get('priority')!r}"
    )
    # And the unedited field must be preserved
    assert state["title"] == "Keep me", (
        f"EDIT event must not change unedited fields; title got {state.get('title')!r}"
    )
