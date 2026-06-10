"""Tests for ticket-reducer.py.

All test functions must FAIL before ticket-reducer.py is implemented.

The reducer is expected to expose a single callable:
    reduce_ticket(ticket_dir_path: Path) -> dict | None

Contract (from src/rebar/_engine/docs/contracts/ticket-event-format.md):
  - Event files are named: <timestamp>-<uuid>-<TYPE>.json
  - Events are sorted lexicographically by filename before reduction.
  - A CREATE event supplies ticket_type, title, and optional parent_id.
  - The reducer returns None if no CREATE event is present or the dir is empty.
  - Exception: a dir with only corrupt/unparseable events returns an error dict
    (status='error') rather than None — ghost-ticket prevention (Test 10).

Test: python3 -m pytest tests/scripts/test_ticket_reducer.py
All tests must return non-zero until ticket-reducer.py is implemented.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import warnings
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
    """Return the ticket-reducer module, failing all tests if absent."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-reducer.py not found at {SCRIPT_PATH} — "
            "this is expected behavior state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID = "3f2a1b4c-5e6d-7f8a-9b0c-1d2e3f4a5b6c"
_UUID2 = "aabbccdd-1122-3344-5566-778899aabbcc"
_UUID3 = "deadbeef-dead-beef-dead-beefdeadbeef"


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


# ---------------------------------------------------------------------------
# Test 1: reducer compiles a single CREATE event to ticket state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_single_create_event_to_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given one CREATE event JSON file, the reducer returns the expected dict."""
    ticket_dir = tmp_path / "tkt-001"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Add reducer",
            "parent_id": "epic-abc",
        },
        env_id="00000000-0000-4000-8000-000000000001",
        author="Alice",
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict for a CREATE event"
    assert state["ticket_id"] == "tkt-001"
    assert state["ticket_type"] == "task"
    assert state["title"] == "Add reducer"
    assert state["status"] == "open", "default status must be 'open'"
    assert state["author"] == "Alice"
    assert state["created_at"] == 1742605200
    assert state["env_id"] == "00000000-0000-4000-8000-000000000001"
    assert state["parent_id"] == "epic-abc"
    assert state["comments"] == []
    assert state["deps"] == []


# ---------------------------------------------------------------------------
# Test 2: reducer sorts events by filename (lexicographic = chronological)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_orders_events_by_filename_not_insertion_order(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Events must be processed in filename-lexicographic order regardless of write order.

    We write the LATER event (t2=1742605300) first and the EARLIER event
    (t1=1742605200) second — simulating a reversed filesystem insertion order.
    The reducer must still apply t1 before t2.

    We verify ordering by writing two CREATE events with the same uuid but
    different timestamps and titles; only the first (t1) must win as CREATE.
    Then a STATUS event at t2 carries the "expected_order_verified" marker we
    assert on.
    """
    ticket_dir = tmp_path / "tkt-order"
    ticket_dir.mkdir()

    # Write t2 event FIRST (later timestamp, written earlier to filesystem)
    _write_event(
        ticket_dir,
        timestamp=1742605300,  # t2 — later
        uuid=_UUID2,
        event_type="STATUS",
        data={
            "status": "closed",
            "current_status": "open",
            "marker": "t2_processed_second",
        },
    )

    # Write t1 event SECOND (earlier timestamp, written later to filesystem)
    _write_event(
        ticket_dir,
        timestamp=1742605200,  # t1 — earlier
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Original title from t1",
            "parent_id": None,
        },
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    # If reducer processes t1 before t2, the title comes from CREATE at t1
    assert state["title"] == "Original title from t1", (
        "reducer must sort events by filename (t1 < t2), not insertion order"
    )
    # Status from t2 STATUS event (applied after CREATE)
    assert state["status"] == "closed", (
        "STATUS event at t2 must be applied after CREATE event at t1"
    )


# ---------------------------------------------------------------------------
# Test 3: reducer skips corrupt JSON with a warning, does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_skips_corrupt_json_with_warning(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given one valid CREATE event and one malformed JSON file, the reducer
    must return valid state from the good event and must NOT raise an exception.
    It should log a warning (any mechanism is acceptable — the test only
    verifies that no exception propagates and valid state is returned).
    """
    ticket_dir = tmp_path / "tkt-corrupt"
    ticket_dir.mkdir()

    # Write a valid CREATE event
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "bug",
            "title": "Reducer is lenient",
            "parent_id": None,
        },
    )

    # Write a malformed JSON file in the same directory
    corrupt_file = ticket_dir / f"1742605300-{_UUID2}-STATUS.json"
    corrupt_file.write_text("{this is not valid json!!!}")

    # The reducer must NOT raise; it should return valid state
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "reduce_ticket must return valid state when a corrupt file is present"
    )
    assert state["title"] == "Reducer is lenient"
    assert state["ticket_type"] == "bug"


# ---------------------------------------------------------------------------
# Test 4: reducer returns None for ticket with no CREATE event
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_returns_none_for_ticket_with_no_create_event(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given a ticket directory that contains only STATUS events (no CREATE),
    reduce_ticket must return None (or raise TicketNotFoundError — either
    signals that the ticket cannot be compiled to state).
    """
    ticket_dir = tmp_path / "tkt-no-create"
    ticket_dir.mkdir()

    # Write a STATUS event with no preceding CREATE
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="STATUS",
        data={"status": "closed"},
    )

    try:
        state = reducer.reduce_ticket(ticket_dir)
        # If no exception, state must be None
        assert state is None, (
            "reduce_ticket must return None when no CREATE event is present"
        )
    except Exception as exc:  # noqa: BLE001
        # TicketNotFoundError or similar is also acceptable
        assert (
            "TicketNotFound" in type(exc).__name__ or "NotFound" in type(exc).__name__
        ), (
            f"Expected TicketNotFoundError or None return, got {type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# Test 5: reducer handles empty ticket directory gracefully
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_handles_empty_ticket_dir(tmp_path: Path, reducer: ModuleType) -> None:
    """Given an existing but empty .tickets-tracker/<ticket_id>/ directory,  # tickets-boundary-ok
    reduce_ticket must return None without crashing.
    """
    ticket_dir = tmp_path / "tkt-empty"
    ticket_dir.mkdir()

    # Directory exists but contains no event files
    state = reducer.reduce_ticket(ticket_dir)

    assert state is None, "reduce_ticket must return None for an empty ticket directory"


# ---------------------------------------------------------------------------
# Test 6: STATUS event updates ticket status (new STATUS contract)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_status_event_to_correct_status(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given a CREATE event followed by a STATUS event, the reducer must update status.

    The STATUS event data includes both 'status' (target) and 'current_status'
    (optimistic concurrency proof). When current_status matches the current
    compiled status, the transition must be applied.
    """
    ticket_dir = tmp_path / "tkt-status"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Status transition test",
            "parent_id": None,
        },
    )

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert state["status"] == "in_progress", (
        "STATUS event must update ticket status when current_status matches"
    )


# ---------------------------------------------------------------------------
# Test 7: STATUS event with current_status mismatch flags conflict
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_applies_multiple_status_events_current_status_mismatch_resolves_fork(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """STATUS event where current_status doesn't match compiled status triggers fork detection.

    The new behavior (DD SC7): when current_status in the event doesn't match the
    compiled state's status, a fork is detected and resolved via lexical UUID tie-break
    on parent_status_uuid rather than accumulating into state['conflicts'].

    Both chains have no parent_status_uuid (empty string tie — incoming wins by <= rule),
    so the incoming event's target_status ("closed") is applied.

    Expected: state is not None, status is resolved (no 'conflicts' key), and
    'conflicts' is not in state.
    """
    ticket_dir = tmp_path / "tkt-conflict"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Conflict detection test",
            "parent_id": None,
        },
    )

    # STATUS event with wrong current_status — ticket is "open" but event says "in_progress"
    # Fork is detected and resolved by tie-break; incoming wins (empty == empty, <= wins).
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "closed", "current_status": "in_progress"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict, not None, on fork"
    # New behavior: fork is resolved via tie-break, never accumulates into 'conflicts'.
    assert "conflicts" not in state, (
        "Fork resolution must not produce a 'conflicts' key; "
        f"got state keys: {list(state.keys())!r}"
    )
    # Incoming event won the tie-break (both parent_status_uuids are empty — incoming <= existing).
    assert state.get("status") == "closed", (
        "Fork tie-break winner's target_status must be applied; "
        f"got state['status']={state.get('status')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_fork_with_empty_existing_uuid_lets_incoming_win(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Bug e60b-e698: when state.parent_status_uuid is empty (no prior fork
    winner has been recorded — e.g. the first fork after CREATE), the
    incoming event MUST win regardless of its UUID's lexical ordering. The
    previous condition ``incoming_uuid <= existing_uuid`` evaluated False for
    any non-empty incoming vs an empty existing (because any string > ""), so
    the existing-wins branch fired and state.status stayed at the loser's
    value. Fix added a `not existing_uuid` guard so empty-existing → incoming
    wins unconditionally.

    This test directly exercises the empty-existing case: a CREATE leaves
    state.parent_status_uuid="" (default). A STATUS event with
    current_status='in_progress' (mismatched) and target='closed' triggers
    fork resolution; without the fix, status stays 'open'; with the fix,
    status becomes 'closed'.
    """
    ticket_dir = tmp_path / "tkt-empty-existing-uuid"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Empty-existing fork test",
            "parent_id": None,
        },
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "closed", "current_status": "in_progress"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return a dict on fork"
    assert state.get("status") == "closed", (
        "Empty existing_uuid must let incoming win — got "
        f"state['status']={state.get('status')!r}"
    )
    # parent_status_uuid must advance to the winner's own UUID (not stay empty
    # and not become the parent pointer) so subsequent forks have a
    # well-defined comparison anchor.
    assert state.get("parent_status_uuid") == _UUID2, (
        "parent_status_uuid must advance to the winning event's own UUID; "
        f"got {state.get('parent_status_uuid')!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: COMMENT event accumulates in comments list
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_comment_event_to_comments_list(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given a CREATE + COMMENT event, the reducer must append to the comments list.

    Each comment in state['comments'] must include at minimum:
      - 'body': the comment text
      - 'author': the event author
      - 'timestamp': the event timestamp
    """
    ticket_dir = tmp_path / "tkt-comment"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Comment test",
            "parent_id": None,
        },
        author="Alice",
    )

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={"body": "first comment"},
        author="Bob",
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 1, (
        "COMMENT event must append one entry to the comments list"
    )
    comment = state["comments"][0]
    assert comment["body"] == "first comment", (
        "comment body must match the COMMENT event data.body"
    )
    assert comment["author"] == "Bob", (
        "comment author must match the COMMENT event author"
    )
    assert comment["timestamp"] == 1742605300, (
        "comment timestamp must match the COMMENT event timestamp"
    )


# ---------------------------------------------------------------------------
# Test 9: Multiple COMMENT events accumulate in chronological order
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_accumulates_multiple_comments(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Given CREATE + two COMMENT events, comments list must have 2 entries in order."""
    ticket_dir = tmp_path / "tkt-multicomment"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Multi-comment test",
            "parent_id": None,
        },
        author="Alice",
    )

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={"body": "first comment"},
        author="Bob",
    )

    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="COMMENT",
        data={"body": "second comment"},
        author="Carol",
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["comments"]) == 2, (
        "Two COMMENT events must produce two entries in comments list"
    )
    assert state["comments"][0]["body"] == "first comment", (
        "First comment must be chronologically first (lower timestamp)"
    )
    assert state["comments"][1]["body"] == "second comment", (
        "Second comment must be chronologically second (higher timestamp)"
    )


# ---------------------------------------------------------------------------
# Test 10: Ghost ticket directory (zero valid events) returns error state dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_returns_error_state_for_ticket_dir_with_zero_valid_events(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A ticket dir containing only corrupt JSON files (no parseable events) must
    return an error state dict — not None, and must not raise.

    Ghost prevention: zero-valid-events → error state, not crash.
    The returned dict must have status='error'.

    # ("returns None if … dir is empty") to differentiate two cases:
    #   - Empty dir (no files at all)     → None  (Tests 4 and 5)
    #   - Corrupt-only dir (no parseable events) → error dict (this test)
    # Story w21-o72z done-definition: ghost tickets must surface as errors,
    # not silently disappear. The updated module docstring now documents this
    # distinction. Returning None for corrupt-only dirs would make ghost
    # tickets invisible to operators, which the story explicitly forbids.
    """
    ticket_dir = tmp_path / "tkt-ghost"
    ticket_dir.mkdir()

    # Write only a corrupt JSON file — no valid events at all
    corrupt_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    corrupt_file.write_text("{this is not valid json at all!!!}")

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "reduce_ticket must return a dict (not None) when only corrupt events exist"
    )
    assert isinstance(state, dict), (
        "reduce_ticket must return a dict for ghost ticket dir"
    )
    assert state.get("status") == "error", (
        f"Ghost ticket dir must return status='error', got status={state.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Test 11: Corrupt CREATE event marks ticket as fsck_needed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_flags_corrupt_create_as_fsck_needed(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A CREATE event missing required fields (ticket_type) must not silently corrupt state.

    The reducer must return a dict with status='fsck_needed' rather than None
    or raising an exception. It must also not block all operations — the
    returned dict must be a non-None, non-raising result.

    # NOTE (w21-o72z): 'fsck_needed' is a new sentinel value introduced by
    # this story to distinguish structurally-corrupt-but-parseable CREATE events
    # (missing required fields) from fully-unparseable corrupt JSON (status='error',
    # Test 10). The sentinel signals: "this ticket exists but needs manual
    # inspection before it can be safely used." The implementer must use
    # exactly 'fsck_needed' as the status string for this case.
    """
    ticket_dir = tmp_path / "tkt-fsck"
    ticket_dir.mkdir()

    # Write a malformed CREATE event — missing the required 'ticket_type' field
    malformed_create: dict = {
        "timestamp": 1742605200,
        "uuid": _UUID,
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Alice",
        "data": {
            # 'ticket_type' is intentionally absent
            "title": "Corrupt create ticket",
            "parent_id": None,
        },
    }
    create_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    create_file.write_text(json.dumps(malformed_create))

    # A STATUS event follows the corrupt CREATE
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "reduce_ticket must return a dict (not None) for a corrupt CREATE event"
    )
    assert isinstance(state, dict), (
        "reduce_ticket must return a dict, not raise, for corrupt CREATE"
    )
    assert state.get("status") == "fsck_needed", (
        f"Corrupt CREATE event must set status='fsck_needed', got status={state.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Test 12: Cache hit — second call with no file changes returns cached state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hit_returns_cached_state(tmp_path: Path, reducer: ModuleType) -> None:
    """Calling reduce_ticket twice with no file changes must serve from cache.

    Without the fix: ticket-reducer.py does not yet implement caching. The assert on
    .cache.json existing will fail because the current implementation never
    writes a cache file.

    Setup: write a CREATE event, call reduce_ticket() once (expected to warm
    the cache and write .cache.json), then call reduce_ticket() again without
    modifying any files.

    Asserts:
      - .cache.json exists in the ticket directory after the first call
      - Second call returns the same state as first (cache hit — same dir_hash)
    """
    ticket_dir = tmp_path / "tkt-cache-hit"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache hit test",
            "parent_id": None,
        },
        author="Alice",
    )

    # First call — expected to warm cache and write .cache.json
    state1 = reducer.reduce_ticket(ticket_dir)

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Second call — no files changed; must return same state (cache hit)
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state1 is not None
    assert state2 is not None
    assert state1 == state2, (
        "Second call with no file changes must return identical state (cache hit)"
    )


# ---------------------------------------------------------------------------
# Test 13: Cache miss on directory listing change (file addition)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_directory_listing_change(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Adding an event file between calls must invalidate the cache.

    Without the fix: without caching, the test structure is valid but the cache-miss
    detection mechanism doesn't exist. Once caching is implemented, a new
    file changes the dir_hash → cache miss → recompute.

    Setup: write a CREATE event, call reduce_ticket() (warms cache), write a
    STATUS event, call reduce_ticket() again.

    Asserts:
      - Second call returns updated state reflecting the STATUS event
      - .cache.json exists (written after first call — after first call)
    """
    ticket_dir = tmp_path / "tkt-cache-miss"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache miss test",
            "parent_id": None,
        },
        author="Alice",
    )

    # First call — warms cache
    state1 = reducer.reduce_ticket(ticket_dir)

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Add a STATUS event — changes directory listing → cache miss
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    # Second call — new file detected; cache invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state1 is not None
    assert state2 is not None
    assert state2["status"] == "in_progress", (
        "After adding a STATUS event, reduce_ticket() must recompute state "
        "and return updated status (cache miss detected)"
    )


# ---------------------------------------------------------------------------
# Test 14: Cache invalidated on file deletion
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_invalidated_on_file_deletion(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Deleting an event file between calls must invalidate the cache.

    Without the fix: without caching, the second call already sees 0 comments because
    the file is gone. However, the assertion that .cache.json is UPDATED
    after the recompute will fail since no cache file is ever written.

    This is critical for w21-q0nn compaction: cache must detect file
    DELETIONS, not just additions.

    Setup: write CREATE + STATUS + COMMENT events, call reduce_ticket()
    (warm cache), delete the COMMENT file, call reduce_ticket() again.

    Asserts:
      - Second call returns state with 0 comments (deletion detected, recomputed)
      - .cache.json exists after first call
      - .cache.json is updated after second call (recompute after cache miss)
    """
    ticket_dir = tmp_path / "tkt-cache-delete"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache deletion test",
            "parent_id": None,
        },
        author="Alice",
    )

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    comment_file = _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="COMMENT",
        data={"body": "a comment that will be deleted"},
        author="Bob",
    )

    # First call — warm cache; state has 1 comment
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert len(state1["comments"]) == 1, "Setup: first call must see the COMMENT event"

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Capture mtime of cache file before deletion-triggered recompute
    mtime_after_warm = cache_file.stat().st_mtime if cache_file.exists() else None

    # Delete the COMMENT file — changes directory listing → cache miss
    comment_file.unlink()

    # Second call — deletion detected; cache invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state2 is not None
    assert len(state2["comments"]) == 0, (
        "After deleting the COMMENT event file, reduce_ticket() must recompute "
        "state and return 0 comments (cache invalidated on file deletion)"
    )

    # Cache file must be updated after recompute (mtime must change)
    assert cache_file.exists(), (
        ".cache.json must still exist after recompute following deletion"
    )
    mtime_after_recompute = cache_file.stat().st_mtime
    assert mtime_after_recompute != mtime_after_warm, (
        ".cache.json must be updated (mtime changed) after cache-miss recompute "
        "triggered by file deletion"
    )


# ---------------------------------------------------------------------------
# Test 15: Warm cache 200 tickets under 500ms
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_200_tickets_under_500ms(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """200 warm-cache reduce_ticket() calls must complete in under 500ms.

    Setup: create 200 ticket directories each with a CREATE event, warm the
    cache by calling reduce_ticket() on each (first pass), then time the
    second pass (all cache hits).

    Marked @pytest.mark.benchmark so this test can be excluded from standard
    unit runs on constrained CI runners: pytest -m "not benchmark".
    """
    ticket_dirs: list[Path] = []
    for i in range(200):
        ticket_dir = tmp_path / f"tkt-{i:04d}"
        ticket_dir.mkdir()
        _write_event(
            ticket_dir,
            timestamp=1742605200 + i,
            uuid=f"00000000-0000-4000-8000-{i:012d}",
            event_type="CREATE",
            data={
                "ticket_type": "task",
                "title": f"Benchmark ticket {i}",
                "parent_id": None,
            },
            author="Bench",
        )
        ticket_dirs.append(ticket_dir)

    # First pass — warm cache (cache miss, OK to be slow)
    for td in ticket_dirs:
        reducer.reduce_ticket(td)

    # Second pass — all cache hits; measure elapsed time
    start = time.monotonic()
    for td in ticket_dirs:
        reducer.reduce_ticket(td)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"200 warm-cache calls took {elapsed:.3f}s, must be < 0.5s"


# ---------------------------------------------------------------------------
# Test 16: Warm cache 1000 tickets under 2s
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_1000_tickets_under_2s(tmp_path: Path, reducer: ModuleType) -> None:
    """1000 warm-cache reduce_ticket() calls must complete in under 2 seconds.

    Setup: create 1000 ticket directories each with a CREATE event, warm the
    cache by calling reduce_ticket() on each (first pass), then time the
    second pass (all cache hits).

    Marked @pytest.mark.benchmark so this test can be excluded from standard
    unit runs on constrained CI runners: pytest -m "not benchmark".
    """
    ticket_dirs: list[Path] = []
    for i in range(1000):
        ticket_dir = tmp_path / f"tkt-{i:04d}"
        ticket_dir.mkdir()
        _write_event(
            ticket_dir,
            timestamp=1742605200 + i,
            uuid=f"00000000-0000-4000-8000-{i:012d}",
            event_type="CREATE",
            data={
                "ticket_type": "task",
                "title": f"Benchmark ticket {i}",
                "parent_id": None,
            },
            author="Bench",
        )
        ticket_dirs.append(ticket_dir)

    # First pass — warm cache (cache miss, OK to be slow)
    for td in ticket_dirs:
        reducer.reduce_ticket(td)

    # Second pass — all cache hits; measure elapsed time
    start = time.monotonic()
    for td in ticket_dirs:
        reducer.reduce_ticket(td)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"1000 warm-cache calls took {elapsed:.3f}s, must be < 2.0s"


# ---------------------------------------------------------------------------
# Test 17: Cache miss on same-filename content change (file overwrite)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_filename_content_change(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Overwriting an event file with different content (same filename) must invalidate cache.

    This test guards against the filename-only hash bug: if the cache hash
    covers only filenames and not file sizes, an in-place overwrite of an
    event file will silently return stale state.

    Setup: write a CREATE event with title "Original title", call
    reduce_ticket() (warms cache), then overwrite the same CREATE event
    file with a different title. Call reduce_ticket() again.

    Asserts:
      - First call returns the original title.
      - Second call (after overwrite) returns the updated title — cache miss.
    """
    ticket_dir = tmp_path / "tkt-content-change"
    ticket_dir.mkdir()

    create_filename = f"1742605200-{_UUID}-CREATE.json"
    create_path = ticket_dir / create_filename

    # Write original CREATE event
    original_payload = {
        "timestamp": 1742605200,
        "uuid": _UUID,
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Alice",
        "data": {
            "ticket_type": "task",
            "title": "Original title",
            "parent_id": None,
        },
    }
    create_path.write_text(json.dumps(original_payload))

    # First call — warm cache
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert state1["title"] == "Original title", (
        "Setup: first call must return the original title"
    )

    # Overwrite same file with updated title (same filename, different content and size)
    updated_payload = {
        **original_payload,
        "data": {
            **original_payload["data"],
            "title": "Updated title after content change",
        },
    }
    create_path.write_text(json.dumps(updated_payload))

    # Second call — content changed; cache must be invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)
    assert state2 is not None
    assert state2["title"] == "Updated title after content change", (
        "After overwriting event file content, reduce_ticket() must recompute state "
        "and return the updated title (cache miss on content change); "
        f"got title={state2['title']!r}"
    )


# ---------------------------------------------------------------------------
# Test 17b: Cache miss on same-SIZE in-place content rewrite (bug 1d76-b6d1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_size_inplace_rewrite(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A same-byte-length in-place rewrite of an event file must invalidate cache.

    Regression guard for bug 1d76-b6d1: the dir-hash keyed on filename+size only
    cannot detect an equal-length overwrite (as produced by a git checkout/rebase
    of the tickets branch or an fsck-recover cherry-pick), so reads served stale
    state. The fix folds st_mtime_ns into the hash.

    Setup: write a CREATE event with a title, warm the cache, then overwrite the
    same file in place with a DIFFERENT title of the SAME byte length and bump
    its mtime (as a checkout would). The next read must reflect the new title.
    Also asserts the cache still HITS on an unchanged dir (no read-path
    regression).
    """
    ticket_dir = tmp_path / "tkt-same-size-rewrite"
    ticket_dir.mkdir()

    create_filename = f"1742605200-{_UUID}-CREATE.json"
    create_path = ticket_dir / create_filename

    # Two titles of identical length -> identical JSON byte length on disk.
    title_a = "AAAAAAAAAA"
    title_b = "BBBBBBBBBB"
    assert len(title_a) == len(title_b)

    def _payload(title: str) -> dict:
        return {
            "timestamp": 1742605200,
            "uuid": _UUID,
            "event_type": "CREATE",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "Alice",
            "data": {"ticket_type": "task", "title": title, "parent_id": None},
        }

    blob_a = json.dumps(_payload(title_a))
    blob_b = json.dumps(_payload(title_b))
    assert len(blob_a) == len(blob_b), "Setup: blobs must be equal byte length"

    create_path.write_text(blob_a)

    # First call — warm cache.
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert state1["title"] == title_a, "Setup: first call must return original title"

    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), ".cache.json must be written after first call"

    # No-change second call MUST hit the cache (cache still effective — no
    # regression): the cache file must not be rewritten.
    cache_mtime_before = cache_file.stat().st_mtime_ns
    state_hit = reducer.reduce_ticket(ticket_dir)
    assert state_hit == state1, "Unchanged dir must serve identical cached state"
    assert cache_file.stat().st_mtime_ns == cache_mtime_before, (
        "Unchanged dir must be a cache HIT (cache file must not be rewritten)"
    )

    # In-place same-size overwrite + mtime bump (simulating a git checkout).
    create_path.write_text(blob_b)
    st = create_path.stat()
    os.utime(create_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert create_path.stat().st_size == st.st_size, "rewrite must be same size"

    # Next read must reflect the new content (cache miss on same-size rewrite).
    state2 = reducer.reduce_ticket(ticket_dir)
    assert state2 is not None
    assert state2["title"] == title_b, (
        "After a same-size in-place rewrite, reduce_ticket() must recompute and "
        f"return the updated title (cache miss on equal-length rewrite); "
        f"got title={state2['title']!r}"
    )


# ---------------------------------------------------------------------------
# Test 18: SNAPSHOT event restores compiled state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_event_restores_compiled_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A SNAPSHOT event with compiled_state in data must restore that state directly.

    Without the fix: ticket-reducer.py does not yet handle SNAPSHOT events. The reducer
    will either ignore the event or raise, causing this test to fail.
    """
    ticket_dir = tmp_path / "tkt-snapshot-basic"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-basic",
        "ticket_type": "task",
        "title": "Compacted title",
        "status": "closed",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
        "source_event_uuids": [_UUID, _UUID2],
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID3,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": [_UUID, _UUID2]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT event must produce non-None state"
    assert state["title"] == "Compacted title", (
        f"SNAPSHOT compiled_state title must be restored; got {state['title']!r}"
    )
    assert state["status"] == "closed", (
        f"SNAPSHOT compiled_state status must be restored; got {state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 19: SNAPSHOT + post-snapshot events applied correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_plus_post_snapshot_events_applied(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A STATUS event after a SNAPSHOT (not in source_event_uuids) must be applied.

    Without the fix: SNAPSHOT handling not yet implemented.
    """
    ticket_dir = tmp_path / "tkt-snapshot-post"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-post",
        "ticket_type": "task",
        "title": "Snapshot base",
        "status": "open",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    # SNAPSHOT at t=1742606000
    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": ["pre-uuid-1"]},
    )

    # Post-snapshot STATUS event at t=1742607000 (uuid NOT in source_event_uuids)
    _write_event(
        ticket_dir,
        timestamp=1742607000,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT + STATUS must produce non-None state"
    assert state["status"] == "closed", (
        "Post-snapshot STATUS event must be applied on top of SNAPSHOT state; "
        f"got status={state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 20: SNAPSHOT deduplicates events in source_event_uuids
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_deduplicates_events_in_source_event_uuids(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An event whose uuid is listed in source_event_uuids must be skipped.

    Without the fix: SNAPSHOT handling and deduplication not yet implemented.
    """
    ticket_dir = tmp_path / "tkt-snapshot-dedup"
    ticket_dir.mkdir()

    dup_uuid = "dup-uuid-0000-0000-0000-000000001234"

    compiled = {
        "ticket_id": "tkt-snapshot-dedup",
        "ticket_type": "task",
        "title": "Dedup test",
        "status": "open",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    # SNAPSHOT listing dup_uuid in source_event_uuids
    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={
            "compiled_state": compiled,
            "source_event_uuids": [dup_uuid],
        },
    )

    # Duplicate event — uuid matches one in source_event_uuids, must be SKIPPED
    _write_event(
        ticket_dir,
        timestamp=1742607000,
        uuid=dup_uuid,
        event_type="STATUS",
        data={"status": "closed", "current_status": "open"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "SNAPSHOT + dup event must produce non-None state"
    assert state["status"] == "open", (
        "Event with uuid in source_event_uuids must be skipped; "
        f"expected status='open' (from SNAPSHOT), got status={state['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 21: SNAPSHOT-only ticket returns compiled state (no CREATE needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_snapshot_only_ticket_returns_compiled_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A ticket with only a SNAPSHOT event (no CREATE) must return the compiled_state.

    Without the fix: SNAPSHOT handling not yet implemented; reducer currently returns None
    when no CREATE event is found.
    """
    ticket_dir = tmp_path / "tkt-snapshot-only"
    ticket_dir.mkdir()

    compiled = {
        "ticket_id": "tkt-snapshot-only",
        "ticket_type": "task",
        "title": "Snapshot only ticket",
        "status": "in_progress",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": "epic-123",
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={"compiled_state": compiled, "source_event_uuids": ["old-1", "old-2"]},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "SNAPSHOT-only ticket must return compiled_state, not None"
    )
    assert state["title"] == "Snapshot only ticket", (
        f"SNAPSHOT compiled_state must be used; got title={state['title']!r}"
    )


# ---------------------------------------------------------------------------
# Test 22: Cache invalidation after compaction (file deletion + SNAPSHOT)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_invalidation_after_compaction_file_deletion(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """After compaction (old files deleted, SNAPSHOT written), cache must invalidate.

    Setup: write CREATE + 3 STATUS events, call reduce_ticket() to warm cache,
    then delete those 4 files and write a SNAPSHOT event (simulating compaction).
    Call reduce_ticket() again and assert the result matches the SNAPSHOT state.

    Without the fix: SNAPSHOT handling not yet implemented. Even if cache invalidation works
    (file count change triggers cache miss), the reducer will fail on the
    SNAPSHOT event type.
    """
    ticket_dir = tmp_path / "tkt-compact-cache"
    ticket_dir.mkdir()

    # Write CREATE + 3 STATUS events
    create_path = _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Pre-compaction title",
            "parent_id": None,
        },
        author="Alice",
    )

    status_paths = []
    for i, (uuid_val, ts) in enumerate(
        [
            ("11111111-1111-1111-1111-111111111111", 1742605300),
            ("22222222-2222-2222-2222-222222222222", 1742605400),
            ("33333333-3333-3333-3333-333333333333", 1742605500),
        ]
    ):
        p = _write_event(
            ticket_dir,
            timestamp=ts,
            uuid=uuid_val,
            event_type="STATUS",
            data={"status": "in_progress", "current_status": "open"},
        )
        status_paths.append(p)

    # Warm cache
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None, "Setup: first reduce must return state"

    # Simulate compaction: delete original files, write SNAPSHOT
    create_path.unlink()
    for p in status_paths:
        p.unlink()

    compacted_state = {
        "ticket_id": "tkt-compact-cache",
        "ticket_type": "task",
        "title": "Compacted title",
        "status": "closed",
        "author": "Alice",
        "created_at": 1742605200,
        "comments": [],
        "deps": [],
        "env_id": "00000000-0000-4000-8000-000000000001",
        "parent_id": None,
    }

    _write_event(
        ticket_dir,
        timestamp=1742606000,
        uuid="44444444-4444-4444-4444-444444444444",
        event_type="SNAPSHOT",
        data={
            "compiled_state": compacted_state,
            "source_event_uuids": [
                _UUID,
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ],
        },
    )

    # Second call — cache must be invalidated (file count changed), SNAPSHOT applied
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state2 is not None, (
        "After compaction, reduce_ticket must return SNAPSHOT compiled_state"
    )
    assert state2["title"] == "Compacted title", (
        "After compaction + cache invalidation, title must come from SNAPSHOT; "
        f"got title={state2['title']!r}"
    )
    assert state2["status"] == "closed", (
        "After compaction, status must come from SNAPSHOT compiled_state; "
        f"got status={state2['status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 23: Integration — warm cache before compaction returns correct state after
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.scripts
def test_integ_cache_warm_before_compaction_returns_correct_state_after(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Warm cache before compaction must be invalidated after compaction runs.

    Setup: write CREATE + 3 STATUS events, warm the cache via reduce_ticket(),
    then simulate compaction (delete all event files, write a SNAPSHOT event).
    Call reduce_ticket() again and verify that:
      - The cache was invalidated (file count changed: 4 events → 1 SNAPSHOT)
      - The returned state reflects the SNAPSHOT compiled_state (not the cached state)

    This validates the end-to-end contract between the caching mechanism
    (w21-f8tg: directory listing hash) and SNAPSHOT handling (w21-vz2h):
    compaction changes both the file count AND the filenames, guaranteeing
    a cache miss via the dir_hash check.
    """
    ticket_dir = tmp_path / "tkt-compact-cache"
    ticket_dir.mkdir()

    # Write a CREATE + 3 STATUS events
    _write_event(
        ticket_dir,
        1742605200,
        _UUID,
        "CREATE",
        {"ticket_type": "task", "title": "Cache test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        1742605201,
        _UUID2,
        "STATUS",
        {"status": "in_progress", "current_status": None},
    )
    _write_event(
        ticket_dir,
        1742605202,
        _UUID3,
        "STATUS",
        {"status": "closed", "current_status": None},
    )
    _write_event(
        ticket_dir,
        1742605203,
        "aaaabbbb-aaaa-bbbb-cccc-ddddeeeeFFFF",
        "STATUS",
        {"status": "open", "current_status": None},
    )

    # Warm the cache
    state_before = reducer.reduce_ticket(ticket_dir)
    assert state_before is not None, (
        "Setup: reduce_ticket must return state after CREATE + STATUS events"
    )
    assert state_before["status"] == "open", (
        f"Setup: expected status='open' from last STATUS event, got {state_before['status']!r}"
    )

    # Verify cache was written after warm
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written after first reduce_ticket() call"
    )

    # Simulate compaction: delete all 4 event files, write SNAPSHOT
    for f in ticket_dir.glob("*.json"):
        if f.name != ".cache.json":
            f.unlink()

    snapshot_payload = {
        "timestamp": 1742605210,
        "uuid": "snapshot-uuid-1234",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Alice",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-compact-cache",
                "ticket_type": "task",
                "title": "Cache test",
                "status": "closed",  # compacted final state
                "author": "Alice",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [],
            },
            "source_event_uuids": [
                _UUID,
                _UUID2,
                _UUID3,
                "aaaabbbb-aaaa-bbbb-cccc-ddddeeeeFFFF",
            ],
        },
    }
    (ticket_dir / "1742605210-snapshot-uuid-1234-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    # After compaction — cache must miss (file count changed) and return SNAPSHOT state
    state_after = reducer.reduce_ticket(ticket_dir)
    assert state_after is not None, (
        "reduce_ticket must return non-None state after compaction (SNAPSHOT present)"
    )
    assert state_after["status"] == "closed", (
        f"Expected status='closed' (SNAPSHOT state), got {state_after['status']!r}"
    )
    assert state_after["title"] == "Cache test", (
        f"Expected title='Cache test' from SNAPSHOT compiled_state, got {state_after['title']!r}"
    )


# ---------------------------------------------------------------------------
# Corrupt-event skip behavior tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_skips_corrupt_json_event_and_returns_valid_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Reducer skips corrupt mid-sequence events and returns valid state."""
    ticket_dir = tmp_path / "tkt-corrupt-skip"
    ticket_dir.mkdir()

    # Valid CREATE event
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Corrupt skip test",
            "parent_id": None,
        },
    )

    # Corrupt event file (invalid JSON)
    corrupt_file = ticket_dir / "1742605250-bad-uuid-STATUS.json"
    corrupt_file.write_text("THIS IS NOT VALID JSON {{{")

    # Valid COMMENT event after the corrupt one
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={"body": "This comment should survive"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state despite corrupt event"
    assert state["ticket_id"] == "tkt-corrupt-skip"
    assert state["title"] == "Corrupt skip test"
    assert len(state["comments"]) == 1, (
        f"Expected 1 comment (corrupt event skipped), got {len(state['comments'])}"
    )
    assert state["comments"][0]["body"] == "This comment should survive"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_emits_warning_for_corrupt_event(
    tmp_path: Path, reducer: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reducer prints WARNING to stderr for corrupt event files."""
    ticket_dir = tmp_path / "tkt-corrupt-warn"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Warning test",
            "parent_id": None,
        },
    )

    corrupt_file = ticket_dir / "1742605250-corrupt-uuid-STATUS.json"
    corrupt_file.write_text("{not valid json at all")

    # Clear any cached state so reducer processes fresh
    cache_file = ticket_dir / ".cache.json"
    if cache_file.exists():
        cache_file.unlink()

    reducer.reduce_ticket(ticket_dir)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err, (
        f"Expected WARNING in stderr, got: {captured.err!r}"
    )
    assert "corrupt" in captured.err.lower() or str(corrupt_file) in captured.err, (
        f"Expected corrupt file path or 'corrupt' in stderr warning, got: {captured.err!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_skips_corrupt_event_in_snapshot_pass1(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Reducer handles corrupt events during SNAPSHOT pass 1 scan gracefully."""
    ticket_dir = tmp_path / "tkt-corrupt-snap"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Snapshot pass1 test",
            "parent_id": None,
        },
    )

    # Corrupt event that pass 1 must skip
    corrupt_file = ticket_dir / "1742605250-corrupt-uuid-STATUS.json"
    corrupt_file.write_text("<<<CORRUPT>>>")

    # Write a SNAPSHOT after the corrupt event
    snapshot_payload = {
        "timestamp": 1742605300,
        "uuid": "snapshot-uuid-pass1",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-corrupt-snap",
                "ticket_type": "task",
                "title": "Snapshot pass1 test",
                "status": "in_progress",
                "author": "Test",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [],
            },
            "source_event_uuids": [_UUID],
        },
    }
    (ticket_dir / "1742605300-snapshot-uuid-pass1-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "reduce_ticket must return state with SNAPSHOT despite corrupt event"
    )
    assert state["status"] == "in_progress", (
        f"Expected status from SNAPSHOT compiled_state, got {state['status']!r}"
    )
    assert state["title"] == "Snapshot pass1 test"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_all_events_corrupt_returns_error_dict(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Dir with only corrupt JSON files returns error dict (ghost ticket prevention)."""
    ticket_dir = tmp_path / "tkt-all-corrupt"
    ticket_dir.mkdir()

    # Write only corrupt event files
    (ticket_dir / "1742605200-bad1-CREATE.json").write_text("NOT JSON 1")
    (ticket_dir / "1742605300-bad2-STATUS.json").write_text("NOT JSON 2")

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, (
        "reduce_ticket must return error dict for all-corrupt dir, not None"
    )
    assert isinstance(state, dict), f"Expected dict, got {type(state)}"
    assert state.get("status") == "error", (
        f"Expected status='error' for all-corrupt dir, got {state.get('status')!r}"
    )
    assert "ticket_id" in state, "Error dict must include ticket_id"


# ---------------------------------------------------------------------------
# Tests: LINK / UNLINK event handling (dso-vwoo)
# These tests MUST FAIL until ticket-reducer.py is extended to handle LINK/UNLINK.
# ---------------------------------------------------------------------------

_LINK_UUID = "11112222-3333-4444-5555-666677778888"
_LINK_UUID2 = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
_LINK_UUID3 = "deadd00d-1234-5678-9abc-def012345678"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_link_event_into_deps_list(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A single LINK event with relation=blocks and target_id=tkt-002 results in
    state['deps'] containing exactly one entry with those fields plus link_uuid."""
    ticket_dir = tmp_path / "tkt-link-single"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "deps" in state, "state must have a 'deps' key"
    assert len(state["deps"]) == 1, (
        f"Expected 1 dep entry, got {len(state['deps'])}: {state['deps']}"
    )
    dep = state["deps"][0]
    assert dep["target_id"] == "tkt-002", (
        f"Expected target_id='tkt-002', got {dep.get('target_id')!r}"
    )
    assert dep["relation"] == "blocks", (
        f"Expected relation='blocks', got {dep.get('relation')!r}"
    )
    assert dep["link_uuid"] == _LINK_UUID, (
        f"Expected link_uuid={_LINK_UUID!r}, got {dep.get('link_uuid')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_event_with_target_key_instead_of_target_id(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """LINK events using 'target' key (legacy format) are accepted and normalized
    to 'target_id' in the compiled state. All 112 existing LINK events on disk use
    'target' rather than 'target_id'. Fix for ticket 9e0f-0828."""
    ticket_dir = tmp_path / "tkt-link-legacy"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Legacy link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "depends_on", "target": "tkt-legacy-001"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert len(state["deps"]) == 1, f"Expected 1 dep, got {len(state['deps'])}"
    dep = state["deps"][0]
    assert dep["target_id"] == "tkt-legacy-001", (
        f"Expected target_id='tkt-legacy-001', got {dep.get('target_id')!r}"
    )
    assert dep["relation"] == "depends_on"
    assert dep["link_uuid"] == _LINK_UUID


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_compiles_multiple_link_events(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Two LINK events produce two independent entries in state['deps']."""
    ticket_dir = tmp_path / "tkt-link-multi"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Multi-link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="LINK",
        data={"relation": "depends_on", "target_id": "tkt-003"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 2, (
        f"Expected 2 dep entries, got {len(state['deps'])}: {state['deps']}"
    )
    link_uuids = {d["link_uuid"] for d in state["deps"]}
    assert _LINK_UUID in link_uuids, "First LINK uuid must be in deps"
    assert _LINK_UUID2 in link_uuids, "Second LINK uuid must be in deps"
    target_ids = {d["target_id"] for d in state["deps"]}
    assert "tkt-002" in target_ids
    assert "tkt-003" in target_ids


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_unlink_event_removes_dep_entry(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A LINK event followed by an UNLINK event with matching link_uuid removes
    the dep entry — state['deps'] is empty after the UNLINK.

    We verify using two ticket dirs: one with LINK only (must show 1 dep) and one
    with LINK + UNLINK (must show 0 deps). This ensures the test cannot pass unless
    LINK events are actually processed.
    """
    # Dir A: LINK only — must produce 1 dep (proves LINK is processed)
    dir_link_only = tmp_path / "tkt-unlink-removes-link-only"
    dir_link_only.mkdir()

    _write_event(
        dir_link_only,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink removes test", "parent_id": None},
    )
    _write_event(
        dir_link_only,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    state_link_only = reducer.reduce_ticket(dir_link_only)
    assert state_link_only is not None
    assert len(state_link_only["deps"]) == 1, (
        f"Precondition: LINK-only dir must have 1 dep, got {state_link_only['deps']}"
    )

    # Dir B: LINK + UNLINK — dep must be removed
    dir_unlinked = tmp_path / "tkt-unlink-removes"
    dir_unlinked.mkdir()

    _write_event(
        dir_unlinked,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink removes test", "parent_id": None},
    )
    _write_event(
        dir_unlinked,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    _write_event(
        dir_unlinked,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="UNLINK",
        data={"link_uuid": _LINK_UUID},
    )

    state = reducer.reduce_ticket(dir_unlinked)

    assert state is not None
    assert state["deps"] == [], f"Expected empty deps after UNLINK, got {state['deps']}"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_unlink_unknown_uuid_is_noop(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An UNLINK event referencing an unknown link_uuid does not crash and leaves
    state['deps'] unchanged."""
    ticket_dir = tmp_path / "tkt-unlink-noop"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Unlink noop test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )
    # UNLINK with a uuid that was never linked
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_LINK_UUID2,
        event_type="UNLINK",
        data={"link_uuid": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None, "reduce_ticket must not raise on unknown UNLINK uuid"
    assert len(state["deps"]) == 1, (
        f"Existing dep must remain after UNLINK with unknown uuid, got {state['deps']}"
    )
    assert state["deps"][0]["link_uuid"] == _LINK_UUID


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_events_survive_snapshot(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A LINK event included in a SNAPSHOT's compiled_state.deps, plus one new LINK
    event after the snapshot, both appear in the final state['deps']."""
    ticket_dir = tmp_path / "tkt-link-snapshot"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Link snapshot test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    # SNAPSHOT captures deps from the LINK above
    snapshot_payload = {
        "timestamp": 1742605400,
        "uuid": "snap-link-uuid-abcd",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-link-snapshot",
                "ticket_type": "task",
                "title": "Link snapshot test",
                "status": "open",
                "author": "Test User",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [
                    {
                        "target_id": "tkt-002",
                        "relation": "blocks",
                        "link_uuid": _LINK_UUID,
                    }
                ],
            },
            "source_event_uuids": [_UUID, _LINK_UUID],
        },
    }
    (ticket_dir / "1742605400-snap-link-uuid-abcd-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    # New LINK event after the snapshot
    _write_event(
        ticket_dir,
        timestamp=1742605500,
        uuid=_LINK_UUID2,
        event_type="LINK",
        data={"relation": "depends_on", "target_id": "tkt-003"},
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 2, (
        f"Expected 2 deps (one from snapshot, one post-snapshot LINK), got {state['deps']}"
    )
    link_uuids = {d["link_uuid"] for d in state["deps"]}
    assert _LINK_UUID in link_uuids, "Dep from snapshot must be preserved"
    assert _LINK_UUID2 in link_uuids, "Post-snapshot LINK must be appended"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_deps_in_snapshot_not_duplicated(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A LINK event listed in a SNAPSHOT's source_event_uuids is not double-counted;
    its dep entry comes only from the compiled_state, not re-applied from the raw event."""
    ticket_dir = tmp_path / "tkt-link-nodupe"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "No-dup link test", "parent_id": None},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_LINK_UUID,
        event_type="LINK",
        data={"relation": "blocks", "target_id": "tkt-002"},
    )

    # SNAPSHOT includes the LINK event in source_event_uuids — reducer must not
    # re-apply the LINK raw event (it is already captured in compiled_state.deps)
    snapshot_payload = {
        "timestamp": 1742605400,
        "uuid": "snap-nodupe-uuid-abcd",
        "event_type": "SNAPSHOT",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {
            "compiled_state": {
                "ticket_id": "tkt-link-nodupe",
                "ticket_type": "task",
                "title": "No-dup link test",
                "status": "open",
                "author": "Test User",
                "created_at": 1742605200,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "parent_id": None,
                "comments": [],
                "deps": [
                    {
                        "target_id": "tkt-002",
                        "relation": "blocks",
                        "link_uuid": _LINK_UUID,
                    }
                ],
            },
            "source_event_uuids": [_UUID, _LINK_UUID],
        },
    }
    (ticket_dir / "1742605400-snap-nodupe-uuid-abcd-SNAPSHOT.json").write_text(
        json.dumps(snapshot_payload)
    )

    state = reducer.reduce_ticket(ticket_dir)

    assert state is not None
    assert len(state["deps"]) == 1, (
        f"Dep must appear exactly once (no double-count), got {state['deps']}"
    )
    assert state["deps"][0]["link_uuid"] == _LINK_UUID
    assert state["ticket_id"] == "tkt-link-nodupe"


# ---------------------------------------------------------------------------
# same-second LINK + UNLINK sort order (dso-jwan)
# LINK must always replay before UNLINK at the same Unix-second timestamp,
# even when the UNLINK filename UUID sorts alphabetically before the LINK UUID.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_same_second_link_unlink_sort_order(
    reducer: ModuleType, tmp_path: Path
) -> None:
    """When LINK and UNLINK share the same Unix-second timestamp, LINK must
    replay before UNLINK so the dep is correctly cancelled.

    Bug scenario (dso-jwan): If filenames sort lexicographically as
    UNLINK < LINK (because UNLINK's UUID precedes LINK's UUID alphabetically),
    the reducer processes UNLINK first — the link_uuid is not yet in deps,
    so UNLINK is a no-op, then LINK adds the dep. The dep appears active when
    it should be cancelled.

    Fix: sort key must be (timestamp_segment, event_type_order, full_name)
    with LINK=0, UNLINK=1, so LINK always processes before UNLINK at the same
    second.
    """
    ticket_dir = tmp_path / "tkt-same-sec"
    ticket_dir.mkdir()

    ts = 1700000000
    link_uuid = "ffff1111-2222-3333-4444-555566667777"  # sorts HIGH alphabetically
    unlink_uuid = "aaaa9999-8888-7777-6666-555544443333"  # sorts LOW alphabetically

    # Write CREATE event
    _write_event(
        ticket_dir,
        timestamp=ts - 10,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Same-second sort test"},
    )

    # Write LINK event: link_uuid sorts HIGH → filename e.g. 1700000000-ffff1111-...-LINK.json
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=link_uuid,
        event_type="LINK",
        data={"target_id": "tkt-target", "relation": "blocks"},
    )

    # Write UNLINK event: unlink_uuid sorts LOW → filename e.g. 1700000000-aaaa9999-...-UNLINK.json
    # Lexicographic sort would put UNLINK before LINK (aaaa < ffff), causing the bug.
    _write_event(
        ticket_dir,
        timestamp=ts,
        uuid=unlink_uuid,
        event_type="UNLINK",
        data={"link_uuid": link_uuid},
    )

    state = reducer.reduce_ticket(str(ticket_dir))

    assert state is not None, "reduce_ticket returned None"
    assert isinstance(state, dict), f"Expected dict, got {type(state)}"
    assert state["deps"] == [], (
        f"Expected empty deps after same-second LINK+UNLINK (UNLINK cancels LINK), "
        f"got {state['deps']!r}. "
        "This indicates UNLINK was processed before LINK (lexicographic sort bug)."
    )


# ---------------------------------------------------------------------------
# Tests: priority and assignee fields in reducer output
# ---------------------------------------------------------------------------

_UUID4 = "11111111-2222-3333-4444-555555555555"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_reads_priority_from_create_event(
    tmp_path: Path, reducer: ModuleType
) -> None:
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
    assert state["priority"] == 2, (
        f"Expected state['priority'] == 2, got {state.get('priority')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_reads_assignee_from_create_event(
    tmp_path: Path, reducer: ModuleType
) -> None:
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
def test_reducer_priority_defaults_to_none_when_absent(
    tmp_path: Path, reducer: ModuleType
) -> None:
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
def test_reducer_assignee_defaults_to_none_when_absent(
    tmp_path: Path, reducer: ModuleType
) -> None:
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
    assert "tags" in state, (
        "state must include a 'tags' field when CREATE event has tags data"
    )
    assert state["tags"] == ["CLI_user"], (
        f"Expected tags=['CLI_user'], got {state.get('tags')!r}"
    )


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
def test_comment_adf_dict_body_coerced_to_string(
    tmp_path: Path, reducer: ModuleType
) -> None:
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
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}
        ],
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
    embedded = (
        '{"checkpoint": 3, "status": "ok", "details": {"files": ["a.py", "b.py"]}}'
    )
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
    assert isinstance(body, str), (
        f"comment body must be a string, not {type(body).__name__!r}"
    )
    assert body == "{}", (
        f"empty dict body must be coerced to '{{}}' via json.dumps, not {body!r} (6bc8-91bc)"
    )


# ---------------------------------------------------------------------------
# Tests: reduce_all_tickets() .archived marker fast-skip (c125-f82e)
#
# T1: fast-skip — .archived marker present + exclude_archived=True → reduce_ticket()
#     is NOT called for that dir (marker detected before reduce_ticket() dispatch).
# T2: slow-path fallback — ARCHIVED event present but NO .archived marker
#     (crash-injection scenario) + exclude_archived=False → reduce_ticket() IS
#     called and returns correct archived=True state.
#
# both tests fail until reduce_all_tickets() is updated to check for a
# .archived marker file before calling reduce_ticket() (fast-skip path).
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_reduce_all_tickets_skips_dir_with_archived_marker(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """reduce_all_tickets(exclude_archived=True) must NOT call reduce_ticket()
    for a ticket directory that has a .archived marker file.

    Without the fix: current implementation calls reduce_ticket() on every directory and
    filters archived tickets only AFTER reduce_ticket() returns.  Once the
    fast-skip path is implemented, the .archived marker is detected before
    reduce_ticket() is called, so the archived ticket never appears in the
    returned results.

    Setup:
      - One ticket directory with an ARCHIVED event AND a .archived marker file.
    When: reduce_all_tickets(tracker_dir, exclude_archived=True) is called.
    Then: the ticket is absent from the returned results entirely (fast-skip).
    """
    import unittest.mock

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-archived-marker"
    ticket_dir.mkdir()

    # Write a valid CREATE + ARCHIVED event sequence
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Archived with marker"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Write the .archived marker file (simulates successful marker write after event)
    (ticket_dir / ".archived").write_text("")

    # Spy on reduce_ticket to detect if it was called for our archived dir
    original_reduce_ticket = reducer.reduce_ticket
    called_dirs: list[str] = []

    def spy_reduce_ticket(
        ticket_dir_path: "str | os.PathLike[str]",
        **kwargs: object,
    ) -> "dict | None":
        called_dirs.append(str(ticket_dir_path))
        return original_reduce_ticket(ticket_dir_path, **kwargs)

    with unittest.mock.patch.object(
        reducer, "reduce_ticket", side_effect=spy_reduce_ticket
    ):
        results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Verify the archived ticket dir was NOT processed by reduce_ticket()
    archived_dir_str = str(ticket_dir)
    assert not any(
        os.path.normpath(d) == os.path.normpath(archived_dir_str) for d in called_dirs
    ), (
        "reduce_all_tickets(exclude_archived=True) must skip calling reduce_ticket() "
        "for directories with a .archived marker file (fast-skip path not implemented); "
        f"reduce_ticket was called for dirs: {called_dirs}"
    )

    # Ticket must not appear in the returned results
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-archived-marker" not in returned_ids, (
        "Ticket with .archived marker must not appear in results when "
        f"exclude_archived=True; got {returned_ids}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reduce_all_tickets_fallback_without_marker_correct_state(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """reduce_all_tickets(exclude_archived=False) must call reduce_ticket() for
    a ticket directory that has an ARCHIVED event but NO .archived marker file,
    and the returned state must have archived=True.

    This verifies the SC 1 correctness fallback: when a crash occurs between
    writing the ARCHIVED event and writing the .archived marker, the slow path
    (full reduce_ticket() replay) still returns the correct archived=True state.

    Setup:
      - One ticket directory with a CREATE event + an ARCHIVED event.
      - NO .archived marker file (simulates crash between event write and marker write).
    When: reduce_all_tickets(tracker_dir, exclude_archived=False) is called.
    Then: reduce_ticket() IS called; result contains the ticket with archived=True.
    """
    import unittest.mock

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-archived-no-marker"
    ticket_dir.mkdir()

    # Write a valid CREATE + ARCHIVED event sequence (NO .archived marker file)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Archived without marker"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Deliberately do NOT create a .archived marker — simulates crash-injection scenario

    # Spy on reduce_ticket to verify it IS called for this dir.
    # reduce_all_tickets lives in ticket_reducer._api, so patch the name there.
    import ticket_reducer._api as _api_mod

    original_reduce_ticket = _api_mod.reduce_ticket
    called_dirs: list[str] = []

    def spy_reduce_ticket(
        ticket_dir_path: "str | os.PathLike[str]",
        **kwargs: object,
    ) -> "dict | None":
        called_dirs.append(str(ticket_dir_path))
        return original_reduce_ticket(ticket_dir_path, **kwargs)

    with unittest.mock.patch.object(
        _api_mod, "reduce_ticket", side_effect=spy_reduce_ticket
    ):
        results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=False)

    # Verify reduce_ticket() WAS called for the crash-scenario dir (slow path)
    archived_dir_str = str(ticket_dir)
    assert any(
        os.path.normpath(d) == os.path.normpath(archived_dir_str) for d in called_dirs
    ), (
        "reduce_all_tickets(exclude_archived=False) must call reduce_ticket() for dirs "
        "with ARCHIVED event but no .archived marker (slow-path fallback); "
        f"reduce_ticket was called for dirs: {called_dirs}"
    )

    # Verify the returned state has archived=True (slow-path correctness)
    assert len(results) == 1, f"Expected 1 result, got {len(results)}: {results}"
    state = results[0]
    assert state.get("ticket_id") == "tkt-archived-no-marker", (
        f"Expected ticket_id='tkt-archived-no-marker', got {state.get('ticket_id')!r}"
    )
    assert state.get("archived") is True, (
        "Ticket with ARCHIVED event but no .archived marker must have archived=True "
        f"in returned state (slow-path fallback correctness); got {state.get('archived')!r}"
    )


# ---------------------------------------------------------------------------
# Tests: compute_dir_hash() is sensitive to .archived marker presence/absence (SC5).
# These tests import compute_dir_hash directly and test the hashing contract.
# compute_dir_hash() includes marker:present/marker:absent in its hash input.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(REPO_ROOT / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ticket_reducer._cache import compute_dir_hash as _compute_dir_hash  # noqa: E402


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_differs_with_marker_present(tmp_path: Path) -> None:
    """Hash must change after an .archived marker is written to the ticket dir.

    Setup: create a ticket dir with one event file. Compute the hash (no marker).
    Write an .archived marker. Compute the hash again.

    Asserts: the two hashes are different (marker presence changes the hash).
    """
    ticket_dir = tmp_path / "tkt-marker-present"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Marker hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Hash without .archived marker
    hash_without_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Write the .archived marker (simulates write_marker())
    (ticket_dir / ".archived").touch()

    # Hash with .archived marker present — must differ
    hash_with_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_without_marker != hash_with_marker, (
        "compute_dir_hash() must return a different hash when .archived marker is present; "
        "compute_dir_hash() must include marker presence in hash (SC5)"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_stable_when_no_marker_change(tmp_path: Path) -> None:
    """Hash must be stable across calls when events and marker state are unchanged.

    This verifies the positive case: no spurious cache invalidation when nothing changes.

    Setup: create a ticket dir with one event file and no marker.
    Compute the hash twice with the same inputs.

    Asserts: both hashes are identical (stable hash with no changes).
    """
    ticket_dir = tmp_path / "tkt-hash-stable"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Stable hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Compute hash twice with identical inputs — must be stable
    hash_first = _compute_dir_hash(ticket_dir_str, event_filenames)
    hash_second = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_first == hash_second, (
        "compute_dir_hash() must return the same hash when called twice with "
        "identical event files and no marker change; got unstable hashes"
    )


# ---------------------------------------------------------------------------
# Tests: reduce_all_tickets() orphan-marker self-heal (96e0-4634)
#
# SC7: Orphan-marker self-heal — .archived marker present but NO *-ARCHIVED.json
#      event file → reduce_all_tickets() removes the stale marker and falls back
#      to slow path, returning correct active state.
# SC8 (cache): After self-heal removes the marker, a second reduce_all_tickets()
#      call also returns correct active state (marker absence propagated to cache).
#
# UPDATE: these tests assert new behavior not yet present in reduce_all_tickets().
# They must FAIL on current code (orphan marker triggers the fast-skip instead
# of self-heal) and pass once the self-heal logic is added.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_orphan_marker_removed_and_slow_path_taken(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """reduce_all_tickets() must detect an orphan .archived marker (no matching
    *-ARCHIVED.json event) and self-heal by removing it, then fall back to slow
    path and return the correct active (non-archived) state.

    UPDATE: currently the fast-skip fires unconditionally when .archived is
    present, returning an empty result list. After self-heal is implemented,
    the marker is removed and the active ticket is returned.

    Setup:
      - One ticket directory with a CREATE event only (active ticket).
      - A stale .archived marker file (orphan — no ARCHIVED event present).
    When: reduce_all_tickets(tracker_dir, exclude_archived=True) is called.
    Then:
      - The .archived marker is removed (self-heal).
      - The ticket IS included in results (slow path returns active state).
      - The returned state has archived=False (or archived absent/None).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-orphan-marker"
    ticket_dir.mkdir()

    # Write only a CREATE event — no ARCHIVED event (active ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Active ticket with orphan marker"},
    )

    # Write a stale .archived marker with NO matching *-ARCHIVED.json event
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Call reduce_all_tickets — self-heal must fire, removing orphan marker
    results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Orphan marker must be removed by self-heal
    assert not marker_path.exists(), (
        "reduce_all_tickets() must remove the stale .archived marker when no "
        "*-ARCHIVED.json event file is present (orphan self-heal not implemented)"
    )

    # Active ticket must appear in results (slow path taken after self-heal)
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-orphan-marker" in returned_ids, (
        "reduce_all_tickets() must include the ticket after self-healing an orphan "
        f"marker and falling back to slow path; got returned_ids={returned_ids}"
    )

    # Returned state must not be archived
    state = next(r for r in results if r.get("ticket_id") == "tkt-orphan-marker")
    assert not state.get("archived"), (
        "After orphan self-heal, returned ticket must not have archived=True; "
        f"got state['archived']={state.get('archived')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_valid_marker_not_removed(tmp_path: Path, reducer: ModuleType) -> None:
    """reduce_all_tickets() must NOT remove a valid .archived marker that has a
    corresponding *-ARCHIVED.json event file.

    This is the complement of the orphan self-heal test: a legitimately archived
    ticket (marker + event both present) must keep its marker intact.

    Setup:
      - One ticket directory with a CREATE event + an ARCHIVED event.
      - A .archived marker (valid — ARCHIVED event is present).
    When: reduce_all_tickets(tracker_dir, exclude_archived=True) is called.
    Then:
      - The .archived marker is NOT removed (no self-heal triggered).
      - The ticket is absent from results (fast-skip still fires).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-valid-marker"
    ticket_dir.mkdir()

    # Write CREATE + ARCHIVED events (legitimate archived ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Legitimately archived"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="ARCHIVED",
        data={},
    )

    # Write .archived marker (valid — ARCHIVED event is present)
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Call reduce_all_tickets — self-heal must NOT fire for a valid marker
    results = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    # Valid marker must remain untouched
    assert marker_path.exists(), (
        "reduce_all_tickets() must NOT remove a valid .archived marker when a "
        "*-ARCHIVED.json event file is present; marker was incorrectly removed"
    )

    # Ticket must be absent from results (fast-skip still applies to valid marker)
    returned_ids = [r.get("ticket_id") for r in results]
    assert "tkt-valid-marker" not in returned_ids, (
        "Ticket with valid .archived marker must be excluded when exclude_archived=True; "
        f"got returned_ids={returned_ids}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_orphan_marker_cache_miss_on_self_heal(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """After orphan self-heal removes .archived marker, a second reduce_all_tickets()
    call must also return the correct active state (cache invalidated by marker removal).

    This tests the SC8 cache-key interaction: compute_dir_hash() includes marker
    presence/absence, so removing the marker during self-heal must cause a cache
    miss on the next call.

    Setup:
      - One ticket directory with a CREATE event only (active ticket).
      - A stale .archived marker (orphan — no ARCHIVED event present).
    When: reduce_all_tickets() is called TWICE (first call self-heals; second call
          must see the updated state, not a stale cache).
    Then:
      - First call: marker is removed, ticket returned as active.
      - Second call: ticket still returned as active (cache miss due to marker removal).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-orphan-cache"
    ticket_dir.mkdir()

    # Write only a CREATE event (active ticket)
    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Cache miss after self-heal"},
    )

    # Write stale .archived marker (no ARCHIVED event)
    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # First call — self-heal fires, marker removed, slow path returns active state
    results_first = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    assert not marker_path.exists(), (
        "First reduce_all_tickets() call must remove the orphan .archived marker "
        "(self-heal not implemented)"
    )

    ids_first = [r.get("ticket_id") for r in results_first]
    assert "tkt-orphan-cache" in ids_first, (
        "First call must return the ticket after orphan self-heal; "
        f"got ids_first={ids_first}"
    )

    # Second call — cache must NOT serve stale archived=True; marker is absent now
    results_second = reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    ids_second = [r.get("ticket_id") for r in results_second]
    assert "tkt-orphan-cache" in ids_second, (
        "Second reduce_all_tickets() call must also return the ticket as active "
        "after orphan self-heal (cache must reflect marker absence); "
        f"got ids_second={ids_second}"
    )

    # Both calls must agree on active (non-archived) state
    state_second = next(
        r for r in results_second if r.get("ticket_id") == "tkt-orphan-cache"
    )
    assert not state_second.get("archived"), (
        "Second call: ticket must not have archived=True after orphan self-heal; "
        f"got state['archived']={state_second.get('archived')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hash_differs_after_marker_removal(tmp_path: Path) -> None:
    """Hash must change again after .archived marker is removed.

    Setup: create a ticket dir with one event file. Write .archived marker.
    Compute hash (with marker). Remove .archived. Compute hash again.

    Asserts:
      - hash_with_marker != hash_without_marker_after_removal (removal changes hash)
      - hash_without_marker_after_removal == original hash_without_marker (symmetric)
    """
    ticket_dir = tmp_path / "tkt-marker-removed"
    ticket_dir.mkdir()

    event_file = ticket_dir / f"1742605200-{_UUID}-CREATE.json"
    event_file.write_text(
        json.dumps(
            {
                "timestamp": 1742605200,
                "uuid": _UUID,
                "event_type": "CREATE",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Alice",
                "data": {"ticket_type": "task", "title": "Marker removal hash test"},
            }
        )
    )

    ticket_dir_str = str(ticket_dir)
    event_filenames = [event_file.name]

    # Baseline hash — no marker
    hash_baseline = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Write .archived marker (simulates write_marker())
    marker_path = ticket_dir / ".archived"
    marker_path.touch()

    hash_with_marker = _compute_dir_hash(ticket_dir_str, event_filenames)

    # Remove .archived marker (simulates remove_marker())
    marker_path.unlink()

    hash_after_removal = _compute_dir_hash(ticket_dir_str, event_filenames)

    assert hash_with_marker != hash_after_removal, (
        "compute_dir_hash() must return a different hash after .archived marker removal"
    )
    assert hash_after_removal == hash_baseline, (
        "compute_dir_hash() hash after marker removal must equal the original "
        f"baseline hash (symmetric); baseline={hash_baseline!r}, "
        f"after_removal={hash_after_removal!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_reverted_archived_marker_is_orphan(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """reduce_all_tickets() must remove a .archived marker when the ARCHIVED event
    has been cancelled by a subsequent REVERT (net non-archived state).

    A ticket with ARCHIVED + REVERT(targeting that ARCHIVED UUID) has a net
    non-archived state: _is_net_archived() must return False and trigger self-heal.

    Note: the slow path still produces archived=True in state (process_revert does
    not undo ARCHIVED — pre-existing behavior, separate from this fix). The marker
    removal is what this test verifies; result set behaviour is not asserted here.

    Setup:
      - One ticket directory with CREATE + ARCHIVED + REVERT(target=ARCHIVED UUID) events.
      - A .archived marker (stale — the ARCHIVED event has been cancelled by REVERT).
    When: reduce_all_tickets(tracker_dir, exclude_archived=True) is called.
    Then:
      - The stale .archived marker is removed by the self-heal logic.
      - The slow path runs (reduce_ticket() is called on the ticket).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    ticket_dir = tracker_dir / "tkt-reverted-archive"
    ticket_dir.mkdir()

    archived_uuid = _UUID2

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Ticket archived then un-archived"},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=archived_uuid,
        event_type="ARCHIVED",
        data={},
    )
    _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="REVERT",
        data={
            "target_event_uuid": archived_uuid,
            "target_event_type": "ARCHIVED",
            "reason": "",
        },
    )

    marker_path = ticket_dir / ".archived"
    marker_path.write_text("")

    # Self-heal must fire and remove the stale marker (REVERT cancels ARCHIVED)
    reducer.reduce_all_tickets(str(tracker_dir), exclude_archived=True)

    assert not marker_path.exists(), (
        "reduce_all_tickets() must remove the .archived marker when the ARCHIVED event "
        "has been cancelled by a REVERT (net non-archived state); marker not removed"
    )


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


# ---------------------------------------------------------------------------
# Test: LINK event alias target normalizes to canonical UUID (bug 8fc3-d3b1)
# ---------------------------------------------------------------------------

_ALIAS_TARGET_UUID = "abcd-1234-5678-abcd"
_ALIAS_LINK_UUID = "bbbb-cccc-dddd-eeee-ffff-0000-1111-2222"
_ALIAS_SOURCE_UUID = "1111-2222-3333-4444-5555-6666-7777-8888"


@pytest.mark.unit
@pytest.mark.scripts
def test_reducer_link_event_alias_target_normalizes_to_canonical_uuid(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """LINK event with a short-hex (alias-form) target_id is resolved to the full
    canonical UUID during reduce_ticket when tracker_dir context is available.

    Without the fix: process_link stores the verbatim short-hex value ("abcd-1234")
    so deps[0]["target_id"] == "abcd-1234" (not the canonical UUID).

    With the fix: reduce_ticket derives tracker_dir and passes it through
    replay_events -> process_link -> resolve_ticket_id, so the stored
    target_id equals the canonical UUID "abcd-1234-5678-abcd".

    RED before fix: assertion `dep['target_id'] == 'abcd-1234-5678-abcd'` fails
    because target_id is still "abcd-1234".
    """
    # Build a 2-ticket tracker
    tracker_dir = tmp_path / "tracker-alias-resolve"
    tracker_dir.mkdir()

    # Target ticket: full canonical-UUID directory
    target_ticket_dir = tracker_dir / _ALIAS_TARGET_UUID
    target_ticket_dir.mkdir()
    _write_event(
        target_ticket_dir,
        timestamp=1742600000,
        uuid="ffff-eeee-dddd-cccc",
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Target ticket", "parent_id": None},
    )

    # Source ticket: LINK event with short-hex target (alias form "abcd-1234")
    source_ticket_dir = tracker_dir / "1111-2222-3333-4444"
    source_ticket_dir.mkdir()
    _write_event(
        source_ticket_dir,
        timestamp=1742601000,
        uuid="aaaa-bbbb-cccc-dddd",
        event_type="CREATE",
        data={"ticket_type": "task", "title": "Source ticket", "parent_id": None},
    )
    _write_event(
        source_ticket_dir,
        timestamp=1742601100,
        uuid="1234-5678-9abc-def0",
        event_type="LINK",
        data={"relation": "blocks", "target_id": "abcd-1234"},
    )

    # Pre-assertion gate: confirm ticket_resolver.resolve_ticket_id is importable and
    # actually maps the short-hex alias to the canonical UUID when called directly.
    # This makes a resolver import/resolution failure unambiguous — the test will
    # skip with a clear reason rather than appearing to test the reducer when the
    # underlying resolver path is silently bypassed by the `except Exception` fallback.
    try:
        import importlib
        import sys as _sys

        # Resolve script directory so ticket_resolver is importable from tests.
        _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        _tr = importlib.import_module("ticket_resolver")
        _resolve_fn = getattr(_tr, "resolve_ticket_id", None)
        assert _resolve_fn is not None, (
            "ticket_resolver.resolve_ticket_id not found — "
            "resolver module exists but lacks the expected function"
        )
        # Verify the resolver maps the alias to the canonical UUID against our tracker.
        resolved = _resolve_fn("abcd-1234", str(tracker_dir))
        assert resolved == _ALIAS_TARGET_UUID, (
            f"ticket_resolver.resolve_ticket_id('abcd-1234', tracker_dir) returned "
            f"{resolved!r}; expected {_ALIAS_TARGET_UUID!r}. "
            "Resolver is available but does not resolve the alias correctly — "
            "fix ticket_resolver before this test can be meaningful."
        )
    except ImportError as exc:
        pytest.skip(
            f"ticket_resolver is not importable ({exc}); cannot verify alias-resolution path. "
            "Install/implement ticket_resolver to un-skip this test."
        )

    state = reducer.reduce_ticket(source_ticket_dir)

    assert state is not None, "reduce_ticket must return state"
    assert "deps" in state, "state must have 'deps' key"
    assert len(state["deps"]) == 1, (
        f"Expected 1 dep entry, got {len(state['deps'])}: {state['deps']}"
    )
    dep = state["deps"][0]
    assert dep["target_id"] == _ALIAS_TARGET_UUID, (
        f"Expected target_id to be resolved to canonical UUID {_ALIAS_TARGET_UUID!r}, "
        f"got {dep.get('target_id')!r}. "
        f"Fix: process_link must resolve short-hex alias to canonical UUID via tracker_dir."
    )
