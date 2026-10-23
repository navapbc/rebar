"""Test graph traversal, readiness, and terminal blocker status."""

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
    """Mark a ticket ready when its sole blocker is closed."""
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
    """Keep a ticket unready while its sole blocker remains open."""
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
    """Any open direct blocker keeps a ticket unready despite closed peers."""
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
    """Treat an absent blocker directory as satisfied.

    The dependent retains its depends_on LINK, but archival or deletion cannot block it
    forever.
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
    """Treat a blocker with a closed tombstone as satisfied."""
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
    """Treat a blocker with a deleted tombstone as terminal and satisfied."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-b", status="open")

    # Keep a CREATE event so reduction sees the blocker. Its deleted tombstone
    # then supplies the terminal status.
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
