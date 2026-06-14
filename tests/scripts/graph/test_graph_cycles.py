"""Cycle detection: direct/transitive, level-scoped, check_cycle_at_level, hidden-dir skip

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
    REPO_ROOT,
    _get_check_cycle_at_level,
    _make_ticket,
    _write_blocks_link,
    _write_link_event,
    _write_ticket,
)

# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_rejects_direct_cycle(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises CyclicDependencyError for a direct cycle A→B, B→A.

    Setup: ticket-a blocks ticket-b already exists.
    Action: add_dependency('ticket-b', 'ticket-a', ...) must raise CyclicDependencyError.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("ticket-b", "ticket-a", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_rejects_transitive_cycle(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises CyclicDependencyError for a transitive cycle A→B→C→A.

    Setup: ticket-a blocks ticket-b, ticket-b blocks ticket-c.
    Action: add_dependency('ticket-c', 'ticket-a', ...) must raise CyclicDependencyError.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-b", "ticket-c", timestamp=1501)

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("ticket-c", "ticket-a", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_allows_dag(graph: ModuleType, tmp_path: Path) -> None:
    """add_dependency does NOT raise for a valid DAG: A→B, A→C, B→D.

    Setup: ticket-a blocks ticket-b, ticket-a blocks ticket-c, ticket-b blocks ticket-d.
    Action: These are all valid DAG edges — no CyclicDependencyError should be raised.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_ticket(tracker_dir, "ticket-d", status="open")

    # Should not raise
    graph.add_dependency("ticket-a", "ticket-b", str(tracker_dir))
    graph.add_dependency("ticket-a", "ticket-c", str(tracker_dir))
    graph.add_dependency("ticket-b", "ticket-d", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_visited_set_prevents_infinite_loop(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Diamond graph (A→B, A→C, B→D, C→D) traverses without infinite recursion.

    Setup:
        - ticket-a blocks ticket-b and ticket-c
        - ticket-b blocks ticket-d
        - ticket-c blocks ticket-d
        - All open

    Expected: build_dep_graph('ticket-d', ...) completes without RecursionError or hang.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")
    _write_ticket(tracker_dir, "ticket-d", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-c", timestamp=1501)
    _write_blocks_link(tracker_dir, "ticket-b", "ticket-d", timestamp=1502)
    _write_blocks_link(tracker_dir, "ticket-c", "ticket-d", timestamp=1503)

    # Must complete without error (visited set prevents re-traversing ticket-a twice)
    result = graph.build_dep_graph("ticket-d", str(tracker_dir))
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"


# ---------------------------------------------------------------------------
# Level-scoped cycle detection wired into add_dependency
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_raises_on_cycle_at_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must raise CyclicDependencyError when a level-scoped cycle would be created.

    Setup:
        - story-a: story, open — blocks story-b
        - story-b: story, open — blocks story-c
        - story-c: story, open

    Action: add_dependency(story-c, story-a, ...) should detect a cycle
    (story-a → story-b → story-c → story-a) at the 'story' level.

    Expected: CyclicDependencyError is raised.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-a", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", ticket_type="story")
    _write_blocks_link(tracker_dir, "story-a", "story-b", timestamp=1500)
    _write_blocks_link(tracker_dir, "story-b", "story-c", timestamp=1501)

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("story-c", "story-a", str(tracker_dir), relation="blocks")


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_raises_on_self_loop_at_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must raise CyclicDependencyError for a self-referential dependency.

    Setup:
        - task-x: task, open

    Action: add_dependency(task-x, task-x, ...) — self-loop.

    Expected: CyclicDependencyError is raised with a message indicating
    self-referential dependency.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "task-x", ticket_type="task")

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("task-x", "task-x", str(tracker_dir), relation="blocks")


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_skips_cycle_check_when_no_type(
    tmp_path: Path,
) -> None:
    """check_cycle_at_level with empty level returns False without BFS traversal.

    This is the fail-open path in add_dependency: when resolved_source_state
    returns ticket_type as None or empty string, level evaluates to "" and
    `if level and check_cycle_at_level(...)` short-circuits — no cycle check runs.

    Tests check_cycle_at_level directly with level="" to verify it returns False
    (fail-open) without accessing the tracker directory.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    check_cycle_at_level = _get_check_cycle_at_level()

    # Empty level string → fail-open: returns False without BFS
    result = check_cycle_at_level("task-a", "task-b", "", str(tracker_dir))
    assert result is False, (
        "check_cycle_at_level must return False for empty level (fail-open)"
    )


# ---------------------------------------------------------------------------
# check_cycle_at_level tests (RED — function does not exist yet)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_detects_cycle(tmp_path: Path) -> None:
    """check_cycle_at_level returns True when adding A→B would close an existing B→A cycle."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    # epic-A and epic-B exist; epic-B has depends_on link to epic-A (B→A)
    # So adding epic-A→epic-B would create a cycle: A→B→A
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    _write_link_event("epic-B", "epic-A", "depends_on", str(tracker))
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_detects_self_loop(tmp_path: Path) -> None:
    """check_cycle_at_level returns True for self-loops (source==target)."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    assert check_cycle_at_level("epic-A", "epic-A", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_case_insensitive(tmp_path: Path) -> None:
    """check_cycle_at_level matches ticket_type case-insensitively."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="Epic")  # capital E
    _make_ticket(tracker, "epic-B", ticket_type="Epic")
    _write_link_event("epic-B", "epic-A", "depends_on", str(tracker))
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is True


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_no_false_positive(tmp_path: Path) -> None:
    """check_cycle_at_level returns False when no cycle exists."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    # A→B exists, but we're checking if adding B→A would cycle — that's True
    # Instead test: no existing links, checking epic-A→epic-B: should be False
    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is False


# ---------------------------------------------------------------------------
# Bug c7a6-96e8: cycle-detection must skip hidden dirs (.suggestions, etc.)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cycle_detection_ignores_suggestions_dir(tmp_path: Path) -> None:
    """Cycle detection via _get_all_blocked_by must not emit 'skipping corrupt event'
    warnings when the tracker dir contains a .suggestions/ subdirectory.

    Bug c7a6-96e8: during `ticket link <src> <tgt> depends_on`, the graph
    traversal iterated all directories in the tracker, including hidden dirs like
    .suggestions/.  Suggestion JSON files lack 'event_type' and are not ticket
    events, so the reducer emitted a WARNING for each one.

    After the fix, hidden directories starting with '.' are skipped entirely.
    """
    import io
    import sys

    _scripts_dir = str(REPO_ROOT / "src" / "rebar" / "_engine")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from rebar.graph._graph import _get_all_blocked_by

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    # Write two regular tickets with no dependency links
    _write_ticket(tracker_dir, "tkt-aaa", status="open")
    _write_ticket(tracker_dir, "tkt-bbb", status="open")

    # Write a .suggestions/ directory with a well-formed suggestion file
    # (schema_version, not event_type — exactly what suggestion-record.sh writes)
    suggestions_dir = tracker_dir / ".suggestions"
    suggestions_dir.mkdir()
    suggestion_payload = {
        "schema_version": 1,
        "timestamp": 1778262194332,
        "session_id": "b25af26e-4e7d-6a27-ad27-4d6a8184522b",
        "source": "stop-hook",
        "observation": "test suggestion",
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
        # _get_all_blocked_by scans the tracker dir for reverse-dep lookup —
        # this is exactly the code path that triggers during cycle detection.
        blocked = _get_all_blocked_by("tkt-aaa", str(tracker_dir))
    finally:
        sys.stderr = old_stderr

    stderr_output = captured_stderr.getvalue()

    # The suggestion file must NOT trigger the "skipping corrupt event" warning
    assert "skipping corrupt event" not in stderr_output, (
        f"Expected no 'skipping corrupt event' warnings, but got:\n{stderr_output}\n"
        "Fix: skip hidden directories (starting with '.') in _get_all_blocked_by."
    )
    # The result must also be correct — no spurious deps from suggestions
    assert "tkt-bbb" not in blocked, (
        f"Expected tkt-bbb not in blocked set (no link exists), got {blocked!r}"
    )
