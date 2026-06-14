"""add_dependency hierarchy integration, redirect JSON, relation-grammar validation

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
    _write_ticket,
)

# ---------------------------------------------------------------------------
# add_dependency hierarchy integration tests (story 983e-7fff)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_cross_story_redirects_to_story_level(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-TIER task→epic dep: add_dependency promotes the task to its epic.

    Under the new type-tier model a task (tier 0) blocking an epic (tier 2) is
    promoted up its parent chain to the nearest epic ancestor so the resulting
    link is epic↔epic.

    Setup:
        - epic-root: epic (no parent)
        - story-a: story with parent_id=epic-root
        - task-a1: task with parent_id=story-a
        - epic-other: epic (the higher-tier endpoint)

    Expected: add_dependency('task-a1', 'epic-other', ...) writes a LINK event for
              epic-root -> epic-other (task promoted to its epic ancestor), not
              task-a1 -> epic-other.
    """
    import glob as _glob

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    graph.add_dependency("task-a1", "epic-other", str(tracker_dir), "depends_on")

    # LINK event must be written in epic-root's directory (promoted source)
    epic_root_dir = tracker_dir / "epic-root"
    link_files = _glob.glob(str(epic_root_dir / "*-LINK.json"))
    assert len(link_files) >= 1, (
        f"Expected LINK event in epic-root dir (promoted source), found: {link_files}"
    )

    # Verify the LINK event targets epic-other (unchanged target)
    found_redirect = False
    for lf in link_files:
        with open(lf) as f:
            ev = json.load(f)
        if ev.get("data", {}).get("target_id") == "epic-other":
            found_redirect = True
            break
    assert found_redirect, (
        "Expected LINK event in epic-root with target_id='epic-other' (promotion), "
        f"but no such event found. Files: {link_files}"
    )

    # No LINK event should be written in task-a1's directory
    task_a1_dir = tracker_dir / "task-a1"
    task_link_files = _glob.glob(str(task_a1_dir / "*-LINK.json"))
    assert len(task_link_files) == 0, (
        f"Expected NO LINK event in task-a1 dir (original source), found: {task_link_files}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_redundant_link_exits_nonzero(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Task-to-direct-parent link is redundant: add_dependency raises ValueError.

    Setup:
        - story-parent: story (no parent)
        - task-child: task with parent_id=story-parent

    Expected: add_dependency('story-parent', 'task-child', ...) raises ValueError
              (is_redundant=True path -- story-parent IS the direct parent of task-child).
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(
        tracker_dir, "task-child", parent_id="story-parent", ticket_type="task"
    )

    with pytest.raises(ValueError, match="redundant"):
        graph.add_dependency("story-parent", "task-child", str(tracker_dir))


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_cross_story_emits_redirect_json_to_stdout(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-TIER promotion: add_dependency prints redirect JSON to stdout.

    Setup: a task under a story under an epic, blocking a separate epic.
    Expected: stdout contains JSON with "redirected": true plus original/resolved
    IDs reflecting the task being promoted to its epic ancestor.
    """
    import contextlib
    import io

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root2", ticket_type="epic")
    _write_ticket(tracker_dir, "story-x", parent_id="epic-root2", ticket_type="story")
    _write_ticket(tracker_dir, "task-x1", parent_id="story-x", ticket_type="task")
    _write_ticket(tracker_dir, "epic-target", ticket_type="epic")

    captured_stdout = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout):
        graph.add_dependency("task-x1", "epic-target", str(tracker_dir), "depends_on")

    stdout_val = captured_stdout.getvalue()
    assert stdout_val.strip(), "Expected redirect JSON on stdout, got empty output"

    try:
        redirect_data = json.loads(stdout_val.strip())
    except json.JSONDecodeError as e:
        pytest.fail(f"stdout is not valid JSON: {e!r}. stdout={stdout_val!r}")

    assert redirect_data.get("redirected") is True, (
        f"Expected 'redirected': true in stdout JSON, got {redirect_data!r}"
    )
    assert redirect_data.get("original", {}).get("source") == "task-x1", (
        f"Expected original.source='task-x1', got {redirect_data!r}"
    )
    assert redirect_data.get("original", {}).get("target") == "epic-target", (
        f"Expected original.target='epic-target', got {redirect_data!r}"
    )
    assert redirect_data.get("resolved", {}).get("source") == "epic-root2", (
        f"Expected resolved.source='epic-root2', got {redirect_data!r}"
    )
    assert redirect_data.get("resolved", {}).get("target") == "epic-target", (
        f"Expected resolved.target='epic-target', got {redirect_data!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_same_parent_still_works(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Same-parent tasks: add_dependency writes LINK normally (no redirect).

    Setup:
        - story-shared: story (no parent)
        - task-p: task with parent_id=story-shared
        - task-q: task with parent_id=story-shared

    Expected: add_dependency('task-p', 'task-q', ...) writes LINK in task-p's
              directory with target_id='task-q' (no redirect -- same parent story).
    """
    import glob as _glob

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-shared", ticket_type="story")
    _write_ticket(tracker_dir, "task-p", parent_id="story-shared", ticket_type="task")
    _write_ticket(tracker_dir, "task-q", parent_id="story-shared", ticket_type="task")

    graph.add_dependency("task-p", "task-q", str(tracker_dir))

    # LINK event must be in task-p's directory (no redirect)
    task_p_dir = tracker_dir / "task-p"
    link_files = _glob.glob(str(task_p_dir / "*-LINK.json"))
    assert len(link_files) >= 1, (
        f"Expected LINK event in task-p dir (same parent, no redirect), found: {link_files}"
    )

    # Verify the LINK targets task-q (original target unchanged)
    found = False
    for lf in link_files:
        with open(lf) as f:
            ev = json.load(f)
        if ev.get("data", {}).get("target_id") == "task-q":
            found = True
            break
    assert found, (
        "Expected LINK event in task-p with target_id='task-q', "
        f"but no such event found. Files: {link_files}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_dep_graph_correctness_after_cross_story_link(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Cross-tier task→story link is promoted to story level; task-level link NOT written.

    Under the type-tier model, a task (tier 0) blocking a story (tier 1) is
    promoted up to the task's own story ancestor, yielding story↔story.

    Given: epic-a → story-x → task-a, and epic-a → story-y
    When: add_dependency('task-a', 'story-y', tracker_dir, 'depends_on')
    Then: story-x has story-y in its deps (task-a promoted to story-x);
          task-a does NOT have story-y in its deps.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "story-x", ticket_type="story", parent_id="epic-a")
    _write_ticket(tracker_dir, "task-a", ticket_type="task", parent_id="story-x")
    _write_ticket(tracker_dir, "story-y", ticket_type="story", parent_id="epic-a")

    graph.add_dependency("task-a", "story-y", str(tracker_dir), "depends_on")

    # Story-level dep should be present (task-a promoted to story-x)
    story_x_deps = graph.build_dep_graph("story-x", str(tracker_dir)).get("deps", [])
    assert any(d["target_id"] == "story-y" for d in story_x_deps), (
        "Expected story-x → story-y dep after cross-tier task→story link"
    )

    # Task-level dep should be absent (NOT written at original task level)
    task_a_deps = graph.build_dep_graph("task-a", str(tracker_dir)).get("deps", [])
    assert not any(d["target_id"] == "story-y" for d in task_a_deps), (
        "Expected NO task-a → story-y dep (should have been promoted to story level)"
    )


# ---------------------------------------------------------------------------
# Relation-grammar validation (Bug 61b8-bb44-fb04-402c)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_rejects_non_canonical_relation(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises ValueError for a non-canonical relation ('blocked_by').

    Bug 61b8-bb44-fb04-402c: the ticket-graph.py --link path (add_dependency in
    _links.py) performed NO validation of the relation argument against the
    canonical set {blocks, depends_on, relates_to, duplicates, supersedes}.  It
    wrote whatever string was passed (e.g. 'blocked_by') verbatim.

    Setup: two open tickets ticket-a and ticket-b.
    Action: add_dependency('ticket-a', 'ticket-b', tracker_dir, 'blocked_by')
    Expected: raises ValueError (not CyclicDependencyError); no LINK event written.
    """
    import glob
    import subprocess

    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-a", status="open")
    _write_ticket(tracker_dir, "ticket-b", status="open")

    subprocess.run(
        ["git", "-C", str(tracker_dir), "init", "-q", "--initial-branch=tickets"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    link_count_before = len(glob.glob(str(tracker_dir / "ticket-a" / "*-LINK.json")))

    with pytest.raises(ValueError, match="blocked_by"):
        graph.add_dependency("ticket-a", "ticket-b", str(tracker_dir), "blocked_by")

    link_count_after = len(glob.glob(str(tracker_dir / "ticket-a" / "*-LINK.json")))
    assert link_count_after == link_count_before, (
        f"Expected no LINK event to be written for non-canonical relation 'blocked_by', "
        f"but found {link_count_after - link_count_before} new LINK event(s). "
        "Fix: add CANONICAL_RELATIONS validation at the top of add_dependency() in _links.py."
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_rejects_all_non_canonical_relations(
    graph: ModuleType, tmp_path: Path
) -> None:
    """add_dependency raises ValueError for every string outside the canonical set.

    Checks a representative sample of non-canonical relation strings:
    'blocked_by', 'is_blocked_by', 'related', 'causes', 'parent', 'child', '', 'BLOCKS'.

    For each: no LINK event must be written to disk.
    """
    import glob
    import subprocess

    non_canonical = [
        "blocked_by",
        "is_blocked_by",
        "related",
        "causes",
        "parent",
        "child",
        "",
        "BLOCKS",  # case mismatch — must also be rejected
    ]

    for bad_relation in non_canonical:
        subdir = tmp_path / f"tracker_{bad_relation or 'empty'}"
        subdir.mkdir()

        _write_ticket(subdir, "src", status="open")
        _write_ticket(subdir, "tgt", status="open")

        subprocess.run(
            ["git", "-C", str(subdir), "init", "-q", "--initial-branch=tickets"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )

        with pytest.raises(ValueError):
            graph.add_dependency("src", "tgt", str(subdir), bad_relation)

        link_files = glob.glob(str(subdir / "src" / "*-LINK.json"))
        assert len(link_files) == 0, (
            f"Expected no LINK event for non-canonical relation {bad_relation!r}, "
            f"but found: {link_files}. "
            "Fix: validate relation in add_dependency() before writing any event."
        )


@pytest.mark.unit
@pytest.mark.scripts
def test_add_dependency_accepts_all_canonical_relations(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_dependency accepts all five canonical relations without raising ValueError.

    Canonical set: blocks, depends_on, relates_to, duplicates, supersedes.
    Each should succeed (no ValueError raised) when two open tickets exist.
    """
    import subprocess

    canonical = ["blocks", "depends_on", "relates_to", "duplicates", "supersedes"]

    for relation in canonical:
        subdir = tmp_path / f"tracker_{relation}"
        subdir.mkdir()

        _write_ticket(subdir, "src", status="open")
        _write_ticket(subdir, "tgt", status="open")

        subprocess.run(
            ["git", "-C", str(subdir), "init", "-q", "--initial-branch=tickets"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(subdir), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(subdir),
                "commit",
                "-q",
                "--no-verify",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
            capture_output=True,
        )

        # Should NOT raise ValueError for canonical relations
        monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
        try:
            graph.add_dependency("src", "tgt", str(subdir), relation)
        except graph.CyclicDependencyError:
            pass  # cycle detection is a different guard — OK
        except ValueError as exc:
            pytest.fail(
                f"add_dependency raised ValueError for canonical relation {relation!r}: {exc}\n"
                "Fix: only reject non-canonical relations, not canonical ones."
            )
