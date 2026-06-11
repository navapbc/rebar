"""Parent-child (children) and archive exclusion

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

from _helpers import (
    REPO_ROOT,
    SCRIPT_PATH,
    _UUID_A,
    _UUID_B,
    _UUID_C,
    _UUID_D,
    _get_check_cycle_at_level,
    _load_module,
    _make_ticket,
    _write_archive_event,
    _write_blocks_link,
    _write_link_event,
    _write_ticket,
)



# ---------------------------------------------------------------------------
# Parent-child (children) tests — bug 8cbf-e13b
# ---------------------------------------------------------------------------


def test_build_dep_graph_includes_children(graph: ModuleType, tmp_path: Path) -> None:
    """build_dep_graph must return a 'children' field listing tickets whose
    parent_id matches the queried ticket.

    Bug 8cbf-e13b: ticket deps returns empty deps for epics with parent-linked
    children because it only traverses dependency links, not parent_id.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Create an epic
    _write_ticket(tracker_dir, "epic-001", ticket_type="epic")
    # Create 3 child stories with parent_id pointing to the epic
    _write_ticket(tracker_dir, "story-a", parent_id="epic-001", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-001", ticket_type="story")
    _write_ticket(tracker_dir, "story-c", parent_id="epic-001", ticket_type="story")
    # Create an unrelated ticket (no parent)
    _write_ticket(tracker_dir, "unrelated")

    result = graph.build_dep_graph("epic-001", str(tracker_dir))

    assert "children" in result, (
        "build_dep_graph result is missing 'children' field — "
        "parent-child relationships are not included in the graph output"
    )
    children = sorted(result["children"])
    assert children == ["story-a", "story-b", "story-c"], (
        f"Expected 3 children [story-a, story-b, story-c], got {children}"
    )


def test_build_dep_graph_children_empty_when_no_children(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must return an empty 'children' list when no tickets
    have parent_id matching the queried ticket."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "lonely-ticket")
    _write_ticket(tracker_dir, "other-ticket")

    result = graph.build_dep_graph("lonely-ticket", str(tracker_dir))

    assert "children" in result, "build_dep_graph result missing 'children' field"
    assert result["children"] == [], (
        f"Expected empty children, got {result['children']}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_compute_archive_eligible_regression(graph: ModuleType, tmp_path: Path) -> None:
    """compute_archive_eligible must still see ALL tickets (including archived) — regression guard.

    GREEN: This test passes today and must continue passing after archived exclusion
    is implemented. Placed BEFORE the RED marker so regressions are caught, not tolerated.

    Setup:
        - ticket-already-archived: closed + ARCHIVED event (already archived)
        - ticket-eligible: closed, no blockers, no dependents (should be eligible)
        - ticket-open: open (seed for BFS — not eligible itself)

    Expected:
        compute_archive_eligible returns ticket-eligible (not ticket-already-archived,
        since it's already archived).
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        # ticket-already-archived: closed and already archived
        _write_ticket(tracker_dir, "ticket-already-archived", status="closed")
        _write_archive_event(tracker_dir, "ticket-already-archived")

        # ticket-eligible: closed, not archived, no open deps — should be eligible
        _write_ticket(tracker_dir, "ticket-eligible", status="closed")

        # ticket-open: open, not linked to anything
        _write_ticket(tracker_dir, "ticket-open", status="open")

        eligible = graph.compute_archive_eligible(str(tracker_dir))

        assert "ticket-eligible" in eligible, (
            f"ticket-eligible should be archive-eligible; got {eligible}. "
            "compute_archive_eligible must still scan all tickets including archived ones."
        )

        assert "ticket-already-archived" not in eligible, (
            f"ticket-already-archived is already archived, must not be re-eligible; "
            f"got {eligible}"
        )

        assert "ticket-open" not in eligible, (
            f"ticket-open is not closed, must not be eligible; got {eligible}"
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_transitive_traversal_includes_archived_midchain(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Transitive blocker traversal must NOT skip archived tickets mid-chain — regression guard.

    GREEN: This test passes today and must continue passing after archived exclusion
    is implemented. Placed BEFORE the RED marker so regressions are caught, not tolerated.

    Setup:
        - ticket-a: open, blocks ticket-b
        - ticket-b: open, ARCHIVED, blocks ticket-c
        - ticket-c: open (the ticket we query)

    Expected: check_would_create_cycle('ticket-c', 'ticket-a', 'blocks', ...) == True
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-a", status="open")
        _write_ticket(tracker_dir, "ticket-b", status="open")
        _write_ticket(tracker_dir, "ticket-c", status="open")
        _write_blocks_link(tracker_dir, "ticket-a", "ticket-b", timestamp=1500)
        _write_blocks_link(tracker_dir, "ticket-b", "ticket-c", timestamp=1501)
        _write_archive_event(tracker_dir, "ticket-b")

        would_cycle = graph.check_would_create_cycle(
            "ticket-c", "ticket-a", "blocks", str(tracker_dir)
        )

        assert would_cycle is True, (
            "check_would_create_cycle must detect cycle through archived mid-chain ticket-b. "
            "Archived exclusion must NOT prune nodes during transitive traversal. "
            f"Got would_cycle={would_cycle!r} (expected True)."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_check_would_create_cycle_no_false_positive_for_transitive_depends_on(
    graph: ModuleType, tmp_path: Path
) -> None:
    """check_would_create_cycle must NOT raise for a redundant transitive depends_on edge.

    Regression guard for bug 122a-6b11: when A depends_on C and C depends_on B,
    the edge A depends_on B is a redundant transitive edge — not a cycle.
    The previous implementation incorrectly flagged this as a cycle because A is
    reachable from B by traversing depends_on edges backward (B←C←A), which is
    the reverse direction and does not constitute a cycle.

    Setup:
        - ticket-a: open
        - ticket-b: open
        - ticket-c: open
        - ticket-a depends_on ticket-c  (A needs C)
        - ticket-c depends_on ticket-b  (C needs B)

    Action: check_would_create_cycle('ticket-a', 'ticket-b', 'depends_on', ...)
            must return False — adding A depends_on B is redundant but not a cycle.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")

    # ticket-a depends_on ticket-c: LINK in ticket-a's dir
    _write_link_event(
        "ticket-a", "ticket-c", "depends_on", str(tracker_dir), timestamp=1500
    )
    # ticket-c depends_on ticket-b: LINK in ticket-c's dir
    _write_link_event(
        "ticket-c", "ticket-b", "depends_on", str(tracker_dir), timestamp=1501
    )

    # Redundant transitive edge: A→B is already implied by A→C→B, but is NOT a cycle.
    would_cycle = graph.check_would_create_cycle(
        "ticket-a", "ticket-b", "depends_on", str(tracker_dir)
    )

    assert would_cycle is False, (
        "check_would_create_cycle returned True for a redundant transitive depends_on edge "
        "(ticket-a→ticket-b when ticket-a→ticket-c→ticket-b already exists). "
        "This is not a cycle — it is a redundant edge that should be allowed. "
        "Bug 122a-6b11: the check was traversing depends_on edges backward, "
        "incorrectly treating A as reachable from B."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_allows_redundant_transitive_depends_on(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency must NOT raise CyclicDependencyError for a redundant transitive edge.

    Regression guard for bug 122a-6b11: ticket link <A> <B> depends_on was
    rejected with 'would create a cycle' when A→C→B already existed.

    Setup:
        - ticket-a, ticket-b, ticket-c: all open
        - ticket-a depends_on ticket-c
        - ticket-c depends_on ticket-b

    Action: add_dependency('ticket-a', 'ticket-b', tracker_dir, 'depends_on')
            must NOT raise — the edge is redundant but valid.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")
    _write_ticket(tracker_dir, "ticket-c", status="open")

    _write_link_event(
        "ticket-a", "ticket-c", "depends_on", str(tracker_dir), timestamp=1500
    )
    _write_link_event(
        "ticket-c", "ticket-b", "depends_on", str(tracker_dir), timestamp=1501
    )

    # Must not raise CyclicDependencyError — redundant transitive edge is allowed
    try:
        graph.add_dependency(
            "ticket-a", "ticket-b", str(tracker_dir), relation="depends_on"
        )
    except graph.CyclicDependencyError as exc:
        pytest.fail(
            f"add_dependency raised CyclicDependencyError for a redundant transitive "
            f"depends_on edge (ticket-a→ticket-b, with ticket-a→ticket-c→ticket-b already "
            f"existing). This is bug 122a-6b11. Error: {exc}"
        )


# ── RED MARKER BOUNDARY ──────────────────────────────────────────────────────
# Tests below this line are expected to FAIL (RED) until archived exclusion is
# implemented in ticket-graph.py. The .test-index RED marker points to the first
# test below (test_build_dep_graph_excludes_archived_children).
# Tests ABOVE this line are GREEN regression guards that must always pass.


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_excludes_archived_children(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must exclude archived tickets from the children list by default.

    Setup:
        - epic-001: open epic
        - story-active: open, parent_id=epic-001 (not archived)
        - story-archived: open, parent_id=epic-001, then ARCHIVED event written

    Expected (default exclude_archived=True):
        result['children'] contains only story-active, not story-archived.

    This test is RED — archived exclusion is not yet implemented.
    To make it GREEN: add exclude_archived parameter to build_dep_graph
    (default True) and filter children by archived status.
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "epic-001", ticket_type="epic")
        _write_ticket(
            tracker_dir, "story-active", parent_id="epic-001", ticket_type="story"
        )
        _write_ticket(
            tracker_dir, "story-archived", parent_id="epic-001", ticket_type="story"
        )
        _write_archive_event(tracker_dir, "story-archived")

        result = graph.build_dep_graph("epic-001", str(tracker_dir))

        assert "children" in result, "build_dep_graph result missing 'children' field"
        assert "story-active" in result["children"], (
            f"story-active should be in children; got {result['children']}"
        )
        assert "story-archived" not in result["children"], (
            f"story-archived (archived) should NOT be in children by default; "
            f"got {result['children']}. "
            "Archived tickets must be excluded from children by default."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_build_dep_graph_excludes_archived_blockers(
    graph: ModuleType, tmp_path: Path
) -> None:
    """build_dep_graph must exclude archived tickets from the blockers list by default.

    Setup:
        - ticket-active-blocker: open, blocks ticket-target
        - ticket-archived-blocker: open, blocks ticket-target, then ARCHIVED event written
        - ticket-target: open

    Expected (default exclude_archived=True):
        result['blockers'] contains only ticket-active-blocker,
        not ticket-archived-blocker.

    This test is RED — archived exclusion in blockers is not yet implemented.
    """
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-active-blocker", status="open")
        _write_ticket(tracker_dir, "ticket-archived-blocker", status="open")
        _write_ticket(tracker_dir, "ticket-target", status="open")
        _write_blocks_link(
            tracker_dir, "ticket-active-blocker", "ticket-target", timestamp=1500
        )
        _write_blocks_link(
            tracker_dir, "ticket-archived-blocker", "ticket-target", timestamp=1501
        )
        _write_archive_event(tracker_dir, "ticket-archived-blocker")

        result = graph.build_dep_graph("ticket-target", str(tracker_dir))

        assert "blockers" in result, "build_dep_graph result missing 'blockers' field"
        assert "ticket-active-blocker" in result["blockers"], (
            f"ticket-active-blocker should be in blockers; got {result['blockers']}"
        )
        assert "ticket-archived-blocker" not in result["blockers"], (
            f"ticket-archived-blocker (archived) should NOT be in blockers by default; "
            f"got {result['blockers']}. "
            "Archived tickets must be excluded from blockers by default."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_deps_cli_include_archived(tmp_path: Path) -> None:
    """ticket-graph.py CLI with --include-archived returns full graph including archived.

    Setup:
        - ticket-parent: epic
        - ticket-child-active: story, parent_id=ticket-parent (not archived)
        - ticket-child-archived: story, parent_id=ticket-parent, ARCHIVED

    Without --include-archived: children = [ticket-child-active] (archived excluded by default)
    With --include-archived: children = [ticket-child-active, ticket-child-archived]

    This test is RED — default archived exclusion is not yet implemented, so the
    without-flag case incorrectly includes the archived child.
    """
    import subprocess
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        _write_ticket(tracker_dir, "ticket-parent", ticket_type="epic")
        _write_ticket(
            tracker_dir,
            "ticket-child-active",
            parent_id="ticket-parent",
            ticket_type="story",
        )
        _write_ticket(
            tracker_dir,
            "ticket-child-archived",
            parent_id="ticket-parent",
            ticket_type="story",
        )
        _write_archive_event(tracker_dir, "ticket-child-archived")

        # First: verify default behavior excludes archived (RED — not yet implemented)
        result_default = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-parent",
                f"--tickets-dir={tracker_dir}",
            ],
            capture_output=True,
            text=True,
        )

        assert result_default.returncode == 0, (
            f"CLI (no flag) exited with {result_default.returncode}; "
            f"stderr={result_default.stderr!r}"
        )
        output_default = json.loads(result_default.stdout)
        children_default = output_default.get("children", [])
        assert "ticket-child-archived" not in children_default, (
            f"Without --include-archived, archived child must be excluded by default; "
            f"children={children_default}. "
            "Default archived exclusion is not yet implemented."
        )

        # Second: verify --include-archived includes the archived child
        result_with_flag = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-parent",
                f"--tickets-dir={tracker_dir}",
                "--include-archived",
            ],
            capture_output=True,
            text=True,
        )

        assert result_with_flag.returncode == 0, (
            f"CLI (--include-archived) exited with {result_with_flag.returncode}; "
            f"stderr={result_with_flag.stderr!r}. "
            "--include-archived flag must be recognized and return exit 0."
        )

        output_with_flag = json.loads(result_with_flag.stdout)
        children_with_flag = output_with_flag.get("children", [])
        assert "ticket-child-archived" in children_with_flag, (
            f"With --include-archived, archived child must appear in result; "
            f"children={children_with_flag}. "
            "--include-archived flag is not yet implemented."
        )
        assert "ticket-child-active" in children_with_flag, (
            f"With --include-archived, active child must still appear; "
            f"children={children_with_flag}"
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)


@pytest.mark.unit
@pytest.mark.scripts
def test_deps_archived_direct_target_error(tmp_path: Path) -> None:
    """CLI: querying deps for an archived ticket directly exits 1 with a helpful message.

    When a user runs `ticket-graph.py <archived-ticket-id> --tickets-dir=...`,
    the ticket exists on disk but is archived. The CLI must:
      - Exit with code 1
      - Print a message to stderr suggesting --include-archived

    This guards against silently returning an empty/stale graph for an archived ticket
    when the user likely needs to use --include-archived.

    This test is RED — the archived-ticket-direct-query guard is not yet implemented.
    """
    import subprocess
    import tempfile

    tracker_dir = Path(tempfile.mkdtemp()) / "tracker"
    tracker_dir.mkdir(parents=True)

    try:
        # Create an archived ticket
        _write_ticket(tracker_dir, "ticket-archived", status="closed")
        _write_archive_event(tracker_dir, "ticket-archived")

        result = subprocess.run(
            [
                "python3",
                str(SCRIPT_PATH),
                "ticket-archived",
                f"--tickets-dir={tracker_dir}",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1, (
            f"CLI must exit 1 when querying an archived ticket directly; "
            f"got returncode={result.returncode}. "
            "The archived-ticket guard is not yet implemented."
        )
        assert "--include-archived" in result.stderr, (
            f"CLI stderr must suggest --include-archived when querying archived ticket; "
            f"got stderr={result.stderr!r}. "
            "The error message must guide users to the correct flag."
        )
    finally:
        import shutil

        shutil.rmtree(str(tracker_dir.parent), ignore_errors=True)
