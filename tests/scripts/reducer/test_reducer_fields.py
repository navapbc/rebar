"""Scalar fields: priority / assignee / tags and COMMENT body coercion

Split from the former monolithic tests/scripts/test_ticket_reducer.py along
reducer-concern seams. The module-under-test fixture (`reducer`) lives in
conftest.py; event-writing helpers (`_write_event`, `_UUID*`) in _events.py.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _write_event

# ---------------------------------------------------------------------------
# Tests: priority and assignee fields in reducer output
# ---------------------------------------------------------------------------

_UUID4 = "11111111-2222-3333-4444-555555555555"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_reads_priority_from_create_event(tmp_path: Path, reducer: ModuleType) -> None:
    """CREATE event with priority in data must surface as state['priority']."""
    ticket_dir = tmp_path / "tkt-priority"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID4,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Test", "priority": 2},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket returned None"
    assert state["priority"] == 2, f"Expected state['priority'] == 2, got {state.get('priority')!r}"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_reads_assignee_from_create_event(tmp_path: Path, reducer: ModuleType) -> None:
    """CREATE event with assignee in data must surface as state['assignee']."""
    ticket_dir = tmp_path / "tkt-assignee"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID4,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Test", "assignee": "Joe Oakhart"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket returned None"
    assert state["assignee"] == "Joe Oakhart", (
        f"Expected state['assignee'] == 'Joe Oakhart', got {state.get('assignee')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_priority_defaults_to_none_when_absent(tmp_path: Path, reducer: ModuleType) -> None:
    """CREATE event without priority must still produce state['priority'] is None."""
    ticket_dir = tmp_path / "tkt-no-priority"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID4,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "No priority"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket returned None"
    assert "priority" in state, "state must contain 'priority' key"
    assert state["priority"] is None, (
        f"Expected state['priority'] is None, got {state['priority']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_assignee_defaults_to_none_when_absent(tmp_path: Path, reducer: ModuleType) -> None:
    """CREATE event without assignee must still produce state['assignee'] is None."""
    ticket_dir = tmp_path / "tkt-no-assignee"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID4,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "No assignee"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket returned None"
    assert "assignee" in state, "state must contain 'assignee' key"
    assert state["assignee"] is None, (
        f"Expected state['assignee'] is None, got {state['assignee']!r}"
    )


# ---------------------------------------------------------------------------
# Test: trailing-slash path must not produce empty ticket_id (b146-4802)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reduce_ticket_trailing_slash_produces_correct_ticket_id(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """reduce_ticket() called with a trailing-slash path must set ticket_id to the directory name."""
    ticket_dir = tmp_path / "tkt-slash"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "bug", "title": "Trailing slash test"},
    )

    # Pass path WITH trailing slash — this is what bash glob */; produces
    state = reducer.reduce_ticket(str(ticket_dir) + "/")

    assert state is not None, "reduce_ticket returned None"
    assert state["ticket_id"] == "tkt-slash", (
        f"Expected ticket_id='tkt-slash', got {state['ticket_id']!r}"
    )


# ---------------------------------------------------------------------------
# Test: CREATE event with tags field populates state.tags
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_create_event_with_tags(tmp_path: Path, reducer: ModuleType) -> None:
    """Given a CREATE event with tags in data, reducer must populate state['tags'].

    Without the fix: ticket-reducer.py currently ignores tags in CREATE events.
    This test will FAIL until the reducer is updated to read tags from event data.
    """
    ticket_dir = tmp_path / "tkt-tags"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "bug",
            "title": "Tags test",
            "parent_id": "",
            "tags": ["CLI_user"],
        },
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict for a CREATE event"
    assert "tags" in state, "state must include a 'tags' field when CREATE event has tags data"
    assert state["tags"] == ["CLI_user"], f"Expected tags=['CLI_user'], got {state.get('tags')!r}"


# ---------------------------------------------------------------------------
# Test: CREATE event without tags field initializes state.tags to empty list
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_create_event_without_tags(tmp_path: Path, reducer: ModuleType) -> None:
    """Given a CREATE event without a tags field, reducer must initialize state['tags'] to [].

    GREEN: the reducer already initializes tags to [] for tickets without tags.
    This test verifies backward compatibility.
    """
    ticket_dir = tmp_path / "tkt-no-tags"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "No tags test",
            "parent_id": "",
        },
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict for a CREATE event"
    assert "tags" in state, "state must include a 'tags' field (default empty list)"
    assert state["tags"] == [], f"Expected tags=[], got {state.get('tags')!r}"


# ---------------------------------------------------------------------------
# Test: COMMENT handler must coerce ADF dict body to string
# When Jira sync writes a COMMENT event with an ADF dict as the body field,
# the reducer must store it as a string so downstream consumers don't receive a dict.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_comment_adf_dict_body_coerced_to_string(tmp_path: Path, reducer: ModuleType) -> None:
    """COMMENT event with ADF dict body must store body as string, not dict.

    Without the fix: currently fails because reducer stores data.get('body', '') verbatim,
    passing through the ADF dict without coercion.
    """
    ticket_dir = tmp_path / "tkt-adf-comment"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "bug", "title": "ADF body test"},
    )
    adf_body = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
    }
    _write_event(
        ticket_dir,
        timestamp=1742605201,
        uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        event_type="COMMENT",
        data={"body": adf_body},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 1, "COMMENT event must produce one comment"
    body = state["comments"][0]["body"]
    assert isinstance(body, str), (
        f"comment body must be a string, not {type(body).__name__!r} — "
        f"ADF dict from Jira sync must be coerced to string (b108-f088)"
    )


# ---------------------------------------------------------------------------
# Test: COMMENT handler round-trips embedded JSON in body
# When a comment body contains a JSON-serialized string (e.g. a CHECKPOINT log),
# the round-trip through reduce_ticket must preserve the body as-is.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_comment_body_with_embedded_json_survives_round_trip(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """COMMENT body containing embedded JSON must survive reduce_ticket round-trip unchanged.

    This covers the case where agents write structured data (e.g. CHECKPOINT JSON)
    as comment bodies. The reducer must not alter string bodies that happen to contain
    JSON content.
    """
    ticket_dir = tmp_path / "tkt-json-comment"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Embedded JSON test"},
    )
    embedded = '{"checkpoint": 3, "status": "ok", "details": {"files": ["a.py", "b.py"]}}'
    _write_event(
        ticket_dir,
        timestamp=1742605201,
        uuid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        event_type="COMMENT",
        data={"body": embedded},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 1
    body = state["comments"][0]["body"]
    assert isinstance(body, str), "body must be a string"
    assert body == embedded, (
        f"Embedded JSON body must survive round-trip unchanged. "
        f"Expected {embedded!r}, got {body!r} (6831-8a22)"
    )


# ---------------------------------------------------------------------------
# Test: COMMENT handler must coerce falsy non-string bodies
# The old guard used `if _raw_body else ""` which treats {} as falsy and
# silently converts it to "" instead of json.dumps({}) = "{}".
# Fix: use explicit `is None` check to distinguish None from other falsy values.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_comment_empty_dict_body_coerced_to_json_string(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """COMMENT event with empty dict body {} must be coerced to '{}', not ''.

    Without the fix: current code uses `if _raw_body else ""` which treats {} as falsy
    and returns '' instead of json.dumps({}) = '{}'. This imprecision loses
    the structural indicator that a non-null body was present (6bc8-91bc).
    Fix: replace `if _raw_body else ""` with explicit `is None` guard.
    """
    ticket_dir = tmp_path / "tkt-empty-dict-body"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "bug", "title": "Empty dict body test"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605201,
        uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        event_type="COMMENT",
        data={"body": {}},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 1
    body = state["comments"][0]["body"]
    assert isinstance(body, str), f"comment body must be a string, not {type(body).__name__!r}"
    assert body == "{}", (
        f"empty dict body must be coerced to '{{}}' via json.dumps, not {body!r} (6bc8-91bc)"
    )
