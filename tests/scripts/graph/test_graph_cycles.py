"""Test direct and transitive cycles, level scope, and hidden-directory exclusion."""

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
def test_graph_cycle_detection_rejects_direct_cycle(graph: ModuleType, tmp_path: Path) -> None:
    """Reject B-to-A when A already blocks B."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_blocks_link(tracker_dir, "ticket-a", "ticket-b")

    with pytest.raises(graph.CyclicDependencyError):
        graph.add_dependency("ticket-b", "ticket-a", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_graph_cycle_detection_rejects_transitive_cycle(graph: ModuleType, tmp_path: Path) -> None:
    """Reject C-to-A when A blocks B and B blocks C."""
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
    """Allow the acyclic edges A-to-B, A-to-C, and B-to-D."""
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
def test_graph_visited_set_prevents_infinite_loop(graph: ModuleType, tmp_path: Path) -> None:
    """Traverse a diamond A-to-B/C-to-D without revisiting forever."""
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
def test_add_dependency_raises_on_cycle_at_level(graph: ModuleType, tmp_path: Path) -> None:
    """Reject a story-level edge closing A-to-B-to-C-to-A."""
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
def test_add_dependency_raises_on_self_loop_at_level(graph: ModuleType, tmp_path: Path) -> None:
    """Reject a task self-loop with a self-reference error."""
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
    """Return False for an empty level without reading the tracker.

    An unresolved source type skips the level-scoped cycle check in add_dependency.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    check_cycle_at_level = _get_check_cycle_at_level()

    # Empty level string → fail-open: returns False without BFS
    result = check_cycle_at_level("task-a", "task-b", "", str(tracker_dir))
    assert result is False, "check_cycle_at_level must return False for empty level (fail-open)"


# Direct check_cycle_at_level coverage follows.


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
    """Skip hidden .suggestions directories during blocked-by traversal.

    Their JSON files are not ticket events and must not produce corruption warnings.
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
        suggestions_dir / "1778262194332-b25af26e-4e7d6a27-ad27-4d6a-8184-5f894b522b0e.json"
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


# ---------------------------------------------------------------------------
# Characterization: _get_all_blocked_by returns an ACCUMULATOR, not `visited`
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_get_all_blocked_by_accumulates_targets_without_a_ticket_dir(tmp_path: Path) -> None:
    """Return discovered blocks targets, including missing target directories.

    The seed is absent unless an edge points back to it. Returning BFS visited instead
    would add the seed and lose dangling targets, changing cycle membership checks.
    """
    from rebar.graph._graph import _get_all_blocked_by

    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "tkt-seed", status="open")
    _write_ticket(tracker_dir, "tkt-real", status="open")
    # tkt-seed blocks a real ticket AND a target that has no ticket directory.
    _write_link_event("tkt-seed", "tkt-real", "blocks", str(tracker_dir))
    _write_link_event("tkt-seed", "tkt-ghost-no-dir", "blocks", str(tracker_dir))

    blocked = _get_all_blocked_by("tkt-seed", str(tracker_dir))

    assert "tkt-ghost-no-dir" in blocked, (
        f"a `blocks` target with no ticket dir must still be accumulated, got {blocked!r}"
    )
    assert "tkt-real" in blocked, f"expected the real blocks target in {blocked!r}"
    assert "tkt-seed" not in blocked, (
        f"the seed must not appear in the result (it is visited, not accumulated), got {blocked!r}"
    )


# ---------------------------------------------------------------------------
# Characterization: check_cycle_at_level refuses to traverse ACROSS levels
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_refuses_a_target_at_another_level(tmp_path: Path) -> None:
    """Do not traverse an edge whose target has a different ticket level.

    A direct cross-level edge to the source would falsely report a cycle without the
    target-side guard.
    """
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    _make_ticket(tracker, "task-M", ticket_type="task")
    _write_link_event("epic-B", "task-M", "depends_on", str(tracker))

    assert check_cycle_at_level("task-M", "epic-B", "epic", str(tracker)) is False, (
        "a dep target whose ticket_type differs from the queried level must not be "
        "traversed into, so task-M is unreachable from epic-B at the 'epic' level"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_check_cycle_at_level_does_not_route_through_a_lower_level_hop(tmp_path: Path) -> None:
    """Stop an epic-level walk at an intervening task-level node."""
    check_cycle_at_level = _get_check_cycle_at_level()
    tracker = tmp_path / ".tickets-tracker"
    _make_ticket(tracker, "epic-A", ticket_type="epic")
    _make_ticket(tracker, "epic-B", ticket_type="epic")
    _make_ticket(tracker, "task-M", ticket_type="task")
    _write_link_event("epic-B", "task-M", "depends_on", str(tracker), timestamp=1500)
    _write_link_event("task-M", "epic-A", "depends_on", str(tracker), timestamp=1501)

    assert check_cycle_at_level("epic-A", "epic-B", "epic", str(tracker)) is False, (
        "an epic → task → epic chain must not count as an epic-level cycle: the "
        "traversal is confined to one level at BOTH ends of every edge"
    )
