"""Graph traversal, ready_to_work, and tombstone/archived-as-closed

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_blocks_link,
    _write_ticket,
)

# ---------------------------------------------------------------------------
# Graph traversal & ready_to_work
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_all_blockers_closed(graph: ModuleType, tmp_path: Path) -> None:
    """Ticket B is ready_to_work=True when its only blocker (A) is closed.

    Setup:
        - ticket-a: closed (blocks ticket-b)
        - ticket-b: open

    Expected: build_dep_graph('ticket-b', tracker_dir)['ready_to_work'] == True
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a is closed), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_blocker_still_open(graph: ModuleType, tmp_path: Path) -> None:
    """Ticket B is ready_to_work=False when its blocker (A) is still open.

    Setup:
        - ticket-a: open (blocks ticket-b)
        - ticket-b: open

    Expected: build_dep_graph('ticket-b', tracker_dir)['ready_to_work'] == False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is False, (
        f"Expected ready_to_work=False (blocker ticket-a is still open), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_ready_to_work_direct_blockers_only(graph: ModuleType, tmp_path: Path) -> None:
    """Ticket B is ready_to_work=False when at least one direct blocker is open.

    Setup:
        - ticket-a: open  (blocks ticket-b)
        - ticket-c: closed (blocks ticket-b)
        - ticket-b: open

    Expected: ready_to_work=False because ticket-a (direct blocker) is still open.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="closed")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-c", "ticket-b", timestamp=1501)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is False, (
        f"Expected ready_to_work=False (ticket-a still open), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_deps_output_schema(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph returns the expected output schema.

    Expected keys: ticket_id, deps, ready_to_work, blockers
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="closed")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "ticket_id" in result, f"Missing 'ticket_id' key in {result!r}"
    assert "deps" in result, f"Missing 'deps' key in {result!r}"
    assert "ready_to_work" in result, f"Missing 'ready_to_work' key in {result!r}"
    assert "blockers" in result, f"Missing 'blockers' key in {result!r}"
    assert result["ticket_id"] == "ticket-b", (
        f"Expected ticket_id='ticket-b', got {result['ticket_id']!r}"
    )
    assert isinstance(result["ready_to_work"], bool), (
        f"ready_to_work must be bool, got {type(result['ready_to_work'])}"
    )
    assert isinstance(result["deps"], list), f"deps must be list, got {type(result['deps'])}"
    assert isinstance(result["blockers"], list), (
        f"blockers must be list, got {type(result['blockers'])}"
    )


# ---------------------------------------------------------------------------
# Tombstone-awareness
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_archived_ticket_treated_as_closed(graph: ModuleType, tmp_path: Path) -> None:
    """A missing blocker directory (archived/tombstoned) is treated as satisfied.

    Setup:
        - ticket-a: directory MISSING (was archived — its dir does not exist)
        - ticket-b: open, has a LINK event (depends_on ticket-a) in ticket-b's own dir

    Since ticket-a's directory is absent, it is treated as closed → ready_to_work=True.

    Note: The LINK event is stored in ticket-b's directory using relation='depends_on'
    with target_id='ticket-a'. This means ticket-b knows it depends on ticket-a, and
    the relationship is discoverable even when ticket-a's directory is absent.
    This tests tombstone resolution: the implementation must treat a missing blocker
    directory as closed, not as an error.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Write ticket-b with a CREATE event and a LINK event (depends_on ticket-a).
    # The LINK lives in ticket-b's directory — ticket-a's directory is never created.
    ticket_b_dir = tracker_dir / "ticket-b"
    ticket_b_dir.mkdir(parents=True)

    create_event = {
        "event_type": "CREATE",
        "uuid": "create-ticket-b",
        "timestamp": 1000,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "ticket_type": "task",
            "title": "Ticket ticket-b",
            "parent_id": None,
        },
    }
    with open(ticket_b_dir / "1000-create-ticket-b-CREATE.json", "w") as f:
        json.dump(create_event, f)

    # ticket-b depends_on ticket-a: LINK event stored in ticket-b's dir.
    # ticket-a's directory does NOT exist (simulates archival / tombstoned blocker).
    link_event = {
        "event_type": "LINK",
        "uuid": "link-ticket-b-depends_on-ticket-a",
        "timestamp": 1500,
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {
            "target_id": "ticket-a",
            "relation": "depends_on",
        },
    }
    with open(ticket_b_dir / "1500-link-ticket-b-depends_on-ticket-a-LINK.json", "w") as f:
        json.dump(link_event, f)

    # ticket-a directory intentionally absent (archived/tombstoned)
    assert not (tracker_dir / "ticket-a").exists(), (
        "ticket-a directory must not exist to simulate archival"
    )

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a directory missing = archived), "
        f"got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_tombstone_tombstone_json_respected(graph: ModuleType, tmp_path: Path) -> None:
    """A blocker with .tombstone.json {'status': 'closed'} is treated as closed.

    Setup:
        - ticket-a: directory exists but contains only .tombstone.json
        - ticket-a blocks ticket-b
        - ticket-b: open

    Expected: ready_to_work=True because .tombstone.json signals closed status.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Create ticket-a directory with only a .tombstone.json
    ticket_a_dir = tracker_dir / "ticket-a"
    ticket_a_dir.mkdir(parents=True)
    tombstone = {"status": "closed", "closed_at": 1700000000}
    with open(ticket_a_dir / ".tombstone.json", "w") as f:
        json.dump(tombstone, f)

    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (.tombstone.json signals closed), got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_ready_to_work_when_blocker_has_deleted_tombstone(
    graph: ModuleType, tmp_path: Path
) -> None:
    """A blocker with .tombstone.json {'status': 'deleted'} is treated as terminal (satisfied).

    Setup:
        - ticket-a: has CREATE event (so reduce_all_tickets includes it) AND
          .tombstone.json {"status": "deleted"}; blocks ticket-b
        - ticket-b: open

    Expected: ready_to_work=True because a "deleted" tombstone status is terminal.

    Regression test: _graph.py treats "deleted" as a terminal status alongside "closed".
    _get_ticket_status returns "deleted" for ticket-a (reads .tombstone.json),
    and the terminal-status check now recognises "deleted" → blocker is terminal
    → ready_to_work=True.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Write ticket-a with a CREATE event so it appears in reduce_all_tickets results,
    # then add .tombstone.json with status="deleted" to simulate a deleted ticket.
    # _get_ticket_status reads .tombstone.json first and returns "deleted",
    # but the current check `status != "closed"` fails to recognise "deleted" as terminal.
    _write_ticket(tracker_dir, "ticket-a", status="open")
    ticket_a_dir = tracker_dir / "ticket-a"
    tombstone = {"status": "deleted", "deleted_at": 1700000000}
    with open(ticket_a_dir / ".tombstone.json", "w") as f:
        json.dump(tombstone, f)

    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)

    result = graph.build_dep_graph("ticket-b", str(tracker_dir))

    assert result["ready_to_work"] is True, (
        f"Expected ready_to_work=True (blocker ticket-a has deleted tombstone), "
        f"got {result!r}. A 'deleted' tombstone status must be treated as terminal, "
        f"not as an open blocker."
    )
