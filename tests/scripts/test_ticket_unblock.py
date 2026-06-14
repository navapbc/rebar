"""RED tests for ticket-unblock.py detect_newly_unblocked function.

These tests are RED — they test functionality that does not yet exist.
All test functions must FAIL before ticket-unblock.py is implemented.

The function under test:
    detect_newly_unblocked(
        closed_ticket_ids: list[str],
        tracker_dir: str,
        event_source: str,
    ) -> list[str]

Contract:
    - Returns the list of ticket IDs that are now ready_to_work=True after
      the given tickets are closed.
    - A ticket is newly unblocked when ALL of its deps are now closed.
    - Accepts event_source values: 'local-close' and 'sync-resolution'.
    - Uses a single batch graph traversal (not one query per closed ticket).

Test: python3 -m pytest tests/scripts/test_ticket_unblock.py
All tests must fail (ERROR/FAILED) until ticket-unblock.py is implemented.
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
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-unblock.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_unblock", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def unblock() -> ModuleType:
    """Return the ticket-unblock module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-unblock.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UUID_A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_UUID_B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_UUID_C = "cccccccc-cccc-4ccc-cccc-cccccccccccc"


def _write_ticket(
    tracker_dir: Path,
    ticket_id: str,
    status: str = "open",
    deps: list[str] | None = None,
) -> Path:
    """Write a minimal ticket directory with a CREATE event (and optional STATUS/LINK events).

    Dependency relationships are recorded as LINK events with relation="blocks" written
    in the blocker's directory, matching the schema used by ticket-link.sh and
    ticket-reducer.py.  For each entry in ``deps``, ticket ``dep_id`` blocks
    ``ticket_id``, so a LINK event is written in ``dep_id``'s directory with
    ``target_id=ticket_id`` and ``relation="blocks"``.

    Filenames follow the convention: ``{timestamp}-{uuid}-{event_type}.json``

    Returns the ticket directory path.
    """
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)

    # CREATE event
    create_event = {
        "event_type": "CREATE",
        "uuid": f"create-{ticket_id}",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": f"Ticket {ticket_id}",
            "parent_id": None,
        },
    }
    with open(ticket_dir / f"1000-create-{ticket_id}-CREATE.json", "w") as f:
        json.dump(create_event, f)

    # STATUS event if not open
    if status != "open":
        status_event = {
            "event_type": "STATUS",
            "uuid": f"status-{ticket_id}",
            "timestamp": 2000,
            "author": "Test User",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "data": {
                "status": status,
                "current_status": "open",
            },
        }
        with open(ticket_dir / f"2000-status-{ticket_id}-STATUS.json", "w") as f:
            json.dump(status_event, f)

    # LINK events: each entry in deps means "dep_id blocks ticket_id".
    # Following ticket-link.sh, the LINK event is written in the blocker's
    # (dep_id's) directory with data.target_id=ticket_id and data.relation=blocks.
    # Ensure the blocker directory exists so the event file can be placed there.
    if deps:
        for i, dep_id in enumerate(deps):
            link_uuid = f"link-{dep_id}-blocks-{ticket_id}-{i:04d}"
            timestamp = 1500 + i
            link_event = {
                "event_type": "LINK",
                "uuid": link_uuid,
                "timestamp": timestamp,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {
                    "target_id": ticket_id,
                    "relation": "blocks",
                },
            }
            blocker_dir = tracker_dir / dep_id
            blocker_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{timestamp}-{link_uuid}-LINK.json"
            with open(blocker_dir / filename, "w") as f:
                json.dump(link_event, f)

    return ticket_dir


# ---------------------------------------------------------------------------
# Test 1: closing A does NOT unblock B when B still depends on C (open)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_no_newly_unblocked_when_blocked_by_other_ticket(
    unblock: ModuleType, tmp_path: Path
) -> None:
    """Closing ticket A must NOT unblock B when B also depends on C (still open).

    Setup:
        - ticket-a: closed (just closed)
        - ticket-b: open, deps=[ticket-a, ticket-c]
        - ticket-c: open

    Expected: detect_newly_unblocked(['ticket-a'], ...) == []
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open", deps=["ticket-a", "ticket-c"])
    _write_ticket(tracker_dir, "ticket-c", status="open")

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    assert result == [], (
        f"Expected no newly unblocked tickets (ticket-c still open), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: closing A unblocks B when B's only blocker was A
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_single_newly_unblocked_on_close(unblock: ModuleType, tmp_path: Path) -> None:
    """Closing ticket A must unblock B when B's only dep was A.

    Setup:
        - ticket-a: closed (just closed)
        - ticket-b: open, deps=[ticket-a]

    Expected: detect_newly_unblocked(['ticket-a'], ...) == ['ticket-b']
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open", deps=["ticket-a"])

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    assert "ticket-b" in result, (
        f"Expected 'ticket-b' to be newly unblocked, got {result!r}"
    )
    assert len(result) == 1, (
        f"Expected exactly 1 newly unblocked ticket, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: closing A unblocks B and C simultaneously
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_multiple_newly_unblocked_on_close(unblock: ModuleType, tmp_path: Path) -> None:
    """Closing ticket A must unblock both B and C when each depended only on A.

    Setup:
        - ticket-a: closed (just closed)
        - ticket-b: open, deps=[ticket-a]
        - ticket-c: open, deps=[ticket-a]

    Expected: detect_newly_unblocked(['ticket-a'], ...) contains 'ticket-b' and 'ticket-c'
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open", deps=["ticket-a"])
    _write_ticket(tracker_dir, "ticket-c", status="open", deps=["ticket-a"])

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    assert "ticket-b" in result, f"Expected 'ticket-b' in result, got {result!r}"
    assert "ticket-c" in result, f"Expected 'ticket-c' in result, got {result!r}"
    assert len(result) == 2, (
        f"Expected exactly 2 newly unblocked tickets, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: batch graph query — traversal called once, not per-ticket
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_graph_query_for_burst(unblock: ModuleType, tmp_path: Path) -> None:
    """detect_newly_unblocked must accept a list and use batch traversal (not per-ticket loops).

    Validates that the function signature accepts a list of closed_ticket_ids
    and processes them in a single pass rather than iterating with individual calls.
    We verify this by passing multiple closed IDs at once and checking correctness —
    a per-ticket implementation would produce duplicates or miscount.

    Setup:
        - ticket-a: closed (batch)
        - ticket-b: closed (batch)
        - ticket-c: open, deps=[ticket-a, ticket-b]  (both blockers now closed)
        - ticket-d: open, deps=[ticket-a]
        - ticket-e: open, deps=[ticket-b]

    Expected: detect_newly_unblocked(['ticket-a', 'ticket-b'], ...) ==
              ['ticket-c', 'ticket-d', 'ticket-e'] (order-independent)
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="closed")
    _write_ticket(tracker_dir, "ticket-c", status="open", deps=["ticket-a", "ticket-b"])
    _write_ticket(tracker_dir, "ticket-d", status="open", deps=["ticket-a"])
    _write_ticket(tracker_dir, "ticket-e", status="open", deps=["ticket-b"])

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a", "ticket-b"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    result_set = set(result)
    expected_set = {"ticket-c", "ticket-d", "ticket-e"}
    assert result_set == expected_set, (
        f"Expected newly unblocked {expected_set!r}, got {result_set!r}"
    )
    # No duplicates
    assert len(result) == len(result_set), f"Result contains duplicates: {result!r}"


# ---------------------------------------------------------------------------
# Test 5: event_source parameter is accepted with valid values
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_event_source_parameter_accepted(unblock: ModuleType, tmp_path: Path) -> None:
    """detect_newly_unblocked must accept event_source='local-close' and 'sync-resolution'.

    Both values must be accepted without raising TypeError or ValueError.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")

    # Should not raise for 'local-close'
    result_local = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )
    assert isinstance(result_local, list), (
        f"Expected list return value for event_source='local-close', got {type(result_local)}"
    )

    # Should not raise for 'sync-resolution'
    result_sync = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="sync-resolution",
    )
    assert isinstance(result_sync, list), (
        f"Expected list return value for event_source='sync-resolution', got {type(result_sync)}"
    )


# ---------------------------------------------------------------------------
# Helpers for depends_on relation
# ---------------------------------------------------------------------------


def _write_depends_on_link(
    tracker_dir: Path,
    depending_id: str,
    blocker_id: str,
    timestamp: int = 1500,
) -> None:
    """Write a LINK event in depending_id's directory: depending_id depends_on blocker_id.

    The LINK event has relation='depends_on' and target_id=blocker_id.
    This means blocker_id must be closed before depending_id can proceed.
    """
    depending_dir = tracker_dir / depending_id
    depending_dir.mkdir(parents=True, exist_ok=True)
    link_uuid = f"link-{depending_id}-depends_on-{blocker_id}"
    link_event = {
        "event_type": "LINK",
        "uuid": link_uuid,
        "timestamp": timestamp,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": blocker_id,
            "relation": "depends_on",
        },
    }
    filename = f"{timestamp}-{link_uuid}-LINK.json"
    with open(depending_dir / filename, "w") as f:
        json.dump(link_event, f)


# ---------------------------------------------------------------------------
# Test 6: depends_on direction — closing the target (blocker) unblocks the dependent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_depends_on_direction_unblocks_dependent(
    unblock: ModuleType, tmp_path: Path
) -> None:
    """Closing ticket A must unblock B when B depends_on A (A is the blocker).

    Setup:
        - ticket-b: open, has LINK event relation='depends_on', target_id='ticket-a'
          (i.e., ticket-b depends on ticket-a, so ticket-a blocks ticket-b)
        - ticket-a: just closed

    Expected: detect_newly_unblocked(['ticket-a'], ...) == ['ticket-b']

    This verifies the depends_on direction: the LINK event is in ticket-b's dir,
    but ticket-a is the blocker (the target of depends_on).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_depends_on_link(tracker_dir, "ticket-b", "ticket-a")

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    assert "ticket-b" in result, (
        f"Expected 'ticket-b' to be newly unblocked (depends_on ticket-a which closed), "
        f"got {result!r}"
    )
    assert len(result) == 1, (
        f"Expected exactly 1 newly unblocked ticket, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_depends_on_does_not_unblock_the_blocker(
    unblock: ModuleType, tmp_path: Path
) -> None:
    """Closing ticket B must NOT treat ticket A as unblocked when B depends_on A.

    Setup:
        - ticket-b: open, depends_on ticket-a (ticket-a blocks ticket-b)
        - ticket-a: open (being "closed" in this test)

    The depends_on LINK is in ticket-b's dir with target_id=ticket-a.
    Closing ticket-a should unblock ticket-b, NOT cause ticket-a to appear unblocked.

    Expected: detect_newly_unblocked(['ticket-a'], ...) contains 'ticket-b', NOT 'ticket-a'
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_depends_on_link(tracker_dir, "ticket-b", "ticket-a")

    result = unblock.detect_newly_unblocked(
        closed_ticket_ids=["ticket-a"],
        tracker_dir=str(tracker_dir),
        event_source="local-close",
    )

    assert "ticket-a" not in result, (
        f"ticket-a must not appear as unblocked (it was closed, not blocked), "
        f"got {result!r}"
    )
    assert "ticket-b" in result, (
        f"Expected 'ticket-b' to be newly unblocked after ticket-a closes, "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# RED tests: batch_close_operations and --batch-close CLI entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_finds_open_children(unblock: ModuleType) -> None:
    """batch_close_operations with a parent having 2 open children returns both IDs.

    Setup:
        - parent: open
        - child-1: open, parent_id=parent
        - child-2: open, parent_id=parent

    Expected: result['open_children'] contains 'child-1' and 'child-2'
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    # Write parent ticket
    parent_dir = tracker_dir / "parent"
    parent_dir.mkdir()
    with open(parent_dir / "1000-create-parent-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-parent",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {"ticket_type": "epic", "title": "Parent", "parent_id": None},
            },
            f,
        )

    # Write child-1 with parent_id=parent
    child1_dir = tracker_dir / "child-1"
    child1_dir.mkdir()
    with open(child1_dir / "1000-create-child1-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-child-1",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {
                    "ticket_type": "task",
                    "title": "Child 1",
                    "parent_id": "parent",
                },
            },
            f,
        )

    # Write child-2 with parent_id=parent
    child2_dir = tracker_dir / "child-2"
    child2_dir.mkdir()
    with open(child2_dir / "1000-create-child2-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-child-2",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {
                    "ticket_type": "task",
                    "title": "Child 2",
                    "parent_id": "parent",
                },
            },
            f,
        )

    result = unblock.batch_close_operations(
        ticket_ids=["parent"],
        tracker_dir=str(tracker_dir),
    )

    open_children = result.get("open_children", [])
    assert "child-1" in open_children, (
        f"Expected 'child-1' in open_children, got {open_children!r}"
    )
    assert "child-2" in open_children, (
        f"Expected 'child-2' in open_children, got {open_children!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_detects_unblocked(unblock: ModuleType) -> None:
    """batch_close_operations: closing a blocker → dependent appears in unblocked list.

    Setup:
        - blocker: open (being closed)
        - dependent: open, blocked by blocker

    Expected: result['newly_unblocked'] contains 'dependent'
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    _write_ticket(tracker_dir, "blocker", status="open")
    _write_ticket(tracker_dir, "dependent", status="open", deps=["blocker"])

    result = unblock.batch_close_operations(
        ticket_ids=["blocker"],
        tracker_dir=str(tracker_dir),
    )

    newly_unblocked = result.get("newly_unblocked", [])
    assert "dependent" in newly_unblocked, (
        f"Expected 'dependent' in newly_unblocked after closing blocker, "
        f"got {newly_unblocked!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_no_false_unblocks(unblock: ModuleType) -> None:
    """batch_close_operations: ticket with remaining open blockers NOT in unblocked list.

    Setup:
        - blocker-a: open (being closed)
        - blocker-b: open (NOT being closed)
        - dependent: open, blocked by both blocker-a and blocker-b

    Expected: result['newly_unblocked'] does NOT contain 'dependent'
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    _write_ticket(tracker_dir, "blocker-a", status="open")
    _write_ticket(tracker_dir, "blocker-b", status="open")
    _write_ticket(
        tracker_dir, "dependent", status="open", deps=["blocker-a", "blocker-b"]
    )

    result = unblock.batch_close_operations(
        ticket_ids=["blocker-a"],
        tracker_dir=str(tracker_dir),
    )

    newly_unblocked = result.get("newly_unblocked", [])
    assert "dependent" not in newly_unblocked, (
        f"'dependent' must not be unblocked (blocker-b still open), "
        f"got {newly_unblocked!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_excludes_archived(unblock: ModuleType) -> None:
    """batch_close_operations: archived child ticket not returned in open_children.

    Setup:
        - parent: open
        - child-open: open, parent_id=parent
        - child-archived: open but ARCHIVED, parent_id=parent

    Expected: result['open_children'] contains 'child-open' but NOT 'child-archived'
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    # Write parent
    parent_dir = tracker_dir / "parent"
    parent_dir.mkdir()
    with open(parent_dir / "1000-create-parent-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-parent",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {"ticket_type": "epic", "title": "Parent", "parent_id": None},
            },
            f,
        )

    # Write child-open with parent_id=parent
    child_open_dir = tracker_dir / "child-open"
    child_open_dir.mkdir()
    with open(child_open_dir / "1000-create-childopen-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-child-open",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {
                    "ticket_type": "task",
                    "title": "Child Open",
                    "parent_id": "parent",
                },
            },
            f,
        )

    # Write child-archived with parent_id=parent and ARCHIVED event
    child_arch_dir = tracker_dir / "child-archived"
    child_arch_dir.mkdir()
    with open(child_arch_dir / "1000-create-childarch-CREATE.json", "w") as f:
        json.dump(
            {
                "event_type": "CREATE",
                "uuid": "create-child-archived",
                "timestamp": 1000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {
                    "ticket_type": "task",
                    "title": "Child Archived",
                    "parent_id": "parent",
                },
            },
            f,
        )
    with open(child_arch_dir / "2000-archived-childarch-ARCHIVED.json", "w") as f:
        json.dump(
            {
                "event_type": "ARCHIVED",
                "uuid": "archived-child-archived",
                "timestamp": 2000,
                "author": "Test User",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "data": {},
            },
            f,
        )

    result = unblock.batch_close_operations(
        ticket_ids=["parent"],
        tracker_dir=str(tracker_dir),
    )

    open_children = result.get("open_children", [])
    assert "child-open" in open_children, (
        f"Expected 'child-open' in open_children, got {open_children!r}"
    )
    assert "child-archived" not in open_children, (
        f"'child-archived' must not be in open_children (it is archived), "
        f"got {open_children!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_empty_tracker(unblock: ModuleType) -> None:
    """batch_close_operations: empty/missing tracker returns empty lists, no error.

    Expected: result['open_children'] == [] and result['newly_unblocked'] == []
    """
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp) / "nonexistent"

    result = unblock.batch_close_operations(
        ticket_ids=["some-ticket"],
        tracker_dir=str(tracker_dir),
    )

    assert result.get("open_children", []) == [], (
        f"Expected empty open_children for missing tracker, "
        f"got {result.get('open_children')!r}"
    )
    assert result.get("newly_unblocked", []) == [], (
        f"Expected empty newly_unblocked for missing tracker, "
        f"got {result.get('newly_unblocked')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_cli_outputs_json(tmp_path: Path) -> None:
    """--batch-close via subprocess produces valid JSON output.

    Invokes ticket-unblock.py with --batch-close and a tracker_dir argument,
    then asserts the stdout is parseable JSON with the expected top-level keys.
    """
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    # Write a minimal ticket so the tracker is not empty
    _write_ticket(tracker_dir, "ticket-x", status="open")

    proc = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "--batch-close",
            str(tracker_dir),
            "ticket-x",
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        f"Expected exit 0 for --batch-close, got {proc.returncode}. "
        f"stderr: {proc.stderr!r}"
    )

    try:
        output = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"--batch-close output is not valid JSON: {exc}\nstdout: {proc.stdout!r}"
        )

    assert "open_children" in output, (
        f"Expected 'open_children' key in JSON output, got {output!r}"
    )
    assert "newly_unblocked" in output, (
        f"Expected 'newly_unblocked' key in JSON output, got {output!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_batch_close_single_reduce_call(unblock: ModuleType) -> None:
    """batch_close_operations calls reduce_all_tickets exactly once (batch traversal).

    Uses unittest.mock to patch reduce_all_tickets and verify it is called
    exactly once regardless of how many ticket_ids are passed.
    """
    import tempfile
    import unittest.mock

    from rebar.graph import _unblock as _pkg_unblock

    tmp = tempfile.mkdtemp()
    tracker_dir = Path(tmp)

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Tier E E3: the unblock logic moved into rebar.graph._unblock, which imports
    # reduce_all_tickets directly (no more _get_reducer importlib shim). Patch it
    # at its new home; the engine ticket-unblock.py re-exports batch_close_operations.
    with unittest.mock.patch.object(
        _pkg_unblock,
        "reduce_all_tickets",
        wraps=_pkg_unblock.reduce_all_tickets,
    ) as mock_reduce:
        unblock.batch_close_operations(
            ticket_ids=["ticket-a", "ticket-b"],
            tracker_dir=str(tracker_dir),
        )
        assert mock_reduce.call_count == 1, (
            f"Expected reduce_all_tickets to be called exactly once, "
            f"got {mock_reduce.call_count} calls"
        )


# ---------------------------------------------------------------------------
# Bug c7a6-96e8: detect_newly_unblocked must skip hidden dirs (.suggestions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_detect_newly_unblocked_ignores_suggestions_dir(
    unblock: ModuleType, tmp_path: Path
) -> None:
    """detect_newly_unblocked must not emit 'skipping corrupt event' warnings
    when the tracker directory contains a .suggestions/ subdirectory.

    Bug c7a6-96e8: the scanner in ticket-unblock.py iterated all directories
    via os.scandir() without filtering hidden directories.  When .suggestions/
    was present, reduce_ticket() was called on it, causing the reducer to emit
    a WARNING for each suggestion file (which lacks 'event_type').

    After the fix, hidden directories (names starting with '.') are skipped.
    """
    import io
    import sys

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Write regular tickets: ticket-a (closed) blocks ticket-b (open)
    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open", deps=["ticket-a"])

    # Write a .suggestions/ directory with a well-formed suggestion file
    # (schema_version, not event_type — exactly what suggestion-record.sh writes)
    suggestions_dir = tracker_dir / ".suggestions"
    suggestions_dir.mkdir()
    suggestion_payload = {
        "schema_version": 1,
        "timestamp": 1778262194332,
        "session_id": "b25af26e-4e7d-6a27-ad27-4d6a8184522b",
        "source": "stop-hook",
        "observation": "test suggestion for bug c7a6-96e8",
    }
    suggestion_file = (
        suggestions_dir
        / "1778262194332-b25af26e-4e7d6a27-ad27-4d6a-8184-5f894b522b0e.json"
    )
    suggestion_file.write_text(json.dumps(suggestion_payload))

    # Capture stderr to detect any spurious warnings
    captured_stderr = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured_stderr
    try:
        result = unblock.detect_newly_unblocked(
            closed_ticket_ids=["ticket-a"],
            tracker_dir=str(tracker_dir),
            event_source="local-close",
        )
    finally:
        sys.stderr = old_stderr

    stderr_output = captured_stderr.getvalue()

    # The suggestion file must NOT trigger the "skipping corrupt event" warning
    assert "skipping corrupt event" not in stderr_output, (
        f"Expected no 'skipping corrupt event' warnings, but got:\n{stderr_output}\n"
        "Fix: skip hidden directories (starting with '.') in the scandir loop."
    )
    # Functional correctness: ticket-b must be unblocked now that ticket-a is closed
    assert "ticket-b" in result, (
        f"Expected 'ticket-b' to be newly unblocked (ticket-a closed), got {result!r}"
    )
