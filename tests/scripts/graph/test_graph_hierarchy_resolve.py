"""Test structural hierarchy resolution and ancestor comparability."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_ticket,
)

# ---------------------------------------------------------------------------
# resolve_hierarchy_link tests (SC1, SC3, SC5, SC10, SC11 + is_redundant)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_same_parent_story_sc1(graph: ModuleType, tmp_path: Path) -> None:
    """Keep sibling tasks unchanged, without redirect or redundancy."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(tracker_dir, "task-a", parent_id="story-parent", ticket_type="task")
    _write_ticket(tracker_dir, "task-b", parent_id="story-parent", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a", "task-b", str(tracker_dir))

    assert result["resolved_source"] == "task-a", (
        f"SC1: expected resolved_source='task-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b", (
        f"SC1: expected resolved_target='task-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC1: expected was_redirected=False, got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC1: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cross_story_same_epic_sc3(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Resolve tasks under separate stories to those stories below their common epic.

    Structural ancestry, not ticket type, determines the redirected endpoints.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b1", parent_id="story-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a1", "task-b1", str(tracker_dir), "blocks")

    assert result["resolved_source"] == "story-a", (
        f"SC3: expected resolved_source='story-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "story-b", (
        f"SC3: expected resolved_target='story-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is True, (
        f"SC3: expected was_redirected=True (escalated to NCA children), "
        f"got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC3: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cross_epic_sc5(graph: ModuleType, tmp_path: Path) -> None:
    """Resolve tasks in disjoint epic trees to their epic roots.

    The virtual root makes parentless epics siblings. Both leaves redirect upward.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "epic-b", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-a", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-b", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b1", parent_id="story-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a1", "task-b1", str(tracker_dir), "depends_on")

    assert result["resolved_source"] == "epic-a", (
        f"SC5: expected resolved_source='epic-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "epic-b", (
        f"SC5: expected resolved_target='epic-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is True, (
        f"SC5: expected was_redirected=True (escalated to the roots), "
        f"got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC5: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_orphan_ticket_sc10(graph: ModuleType, tmp_path: Path) -> None:
    """Keep two parentless tickets unchanged without redirect."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "orphan-a", ticket_type="task")
    _write_ticket(tracker_dir, "orphan-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("orphan-a", "orphan-b", str(tracker_dir))

    assert result["resolved_source"] == "orphan-a", (
        f"SC10: expected resolved_source='orphan-a', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "orphan-b", (
        f"SC10: expected resolved_target='orphan-b', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC10: expected was_redirected=False, got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC10: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_unreadable_ticket_sc11(graph: ModuleType, tmp_path: Path) -> None:
    """Return an error for an endpoint that cannot be reduced."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "ticket-ok", ticket_type="task")
    # missing-ticket directory is intentionally absent

    result = graph.resolve_hierarchy_link("ticket-ok", "missing-ticket", str(tracker_dir))

    assert "error" in result, (
        f"SC11: expected result to contain 'error' key for missing ticket, got {result!r}"
    )
    assert result.get("ticket_id") == "missing-ticket", (
        f"SC11: expected ticket_id='missing-ticket' in error, got {result.get('ticket_id')!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_is_redundant_direct_parent(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Mark a direct parent-child pair redundant."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "story-parent", ticket_type="story")
    _write_ticket(tracker_dir, "task-child", parent_id="story-parent", ticket_type="task")

    result = graph.resolve_hierarchy_link("story-parent", "task-child", str(tracker_dir))

    assert result["is_redundant"] is True, (
        f"is_redundant=True expected when source is direct parent of target, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize("relation", ["relates_to", "duplicates", "supersedes", "discovered_from"])
def test_resolve_hierarchy_link_non_blocking_never_promoted(
    graph: ModuleType, tmp_path: Path, relation: str
) -> None:
    """Keep exact endpoints for non-blocking relations across hierarchy levels."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    result = graph.resolve_hierarchy_link("task-leaf", "epic-other", str(tracker_dir), relation)

    assert result["resolved_source"] == "task-leaf", result
    assert result["resolved_target"] == "epic-other", result
    assert result["was_redirected"] is False, (
        f"{relation}: expected was_redirected=False, got {result!r}"
    )
    assert result["is_redundant"] is False, result


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize("relation", ["blocks", "depends_on"])
def test_resolve_hierarchy_link_task_to_epic_promotes_to_epic(
    graph: ModuleType, tmp_path: Path, relation: str
) -> None:
    """Promote a task-to-epic blocking edge to the task's epic ancestor."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "epic-other", ticket_type="epic")

    # source is the lower-tier endpoint
    result = graph.resolve_hierarchy_link("task-leaf", "epic-other", str(tracker_dir), relation)
    assert result["resolved_source"] == "epic-root", result
    assert result["resolved_target"] == "epic-other", result
    assert result["was_redirected"] is True, result

    # symmetric: lower-tier endpoint as TARGET is promoted too
    result_rev = graph.resolve_hierarchy_link("epic-other", "task-leaf", str(tracker_dir), relation)
    assert result_rev["resolved_source"] == "epic-other", result_rev
    assert result_rev["resolved_target"] == "epic-root", result_rev
    assert result_rev["was_redirected"] is True, result_rev


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_task_to_story_promotes_to_story(
    graph: ModuleType, tmp_path: Path
) -> None:
    """(b') Task↔story blocking dep promotes the task only to its STORY ancestor.

    Confirms promotion targets the HIGHER endpoint's tier (story), not all the
    way to the epic root.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")
    _write_ticket(tracker_dir, "story-other", parent_id="epic-root", ticket_type="story")

    result = graph.resolve_hierarchy_link("task-leaf", "story-other", str(tracker_dir), "blocks")
    assert result["resolved_source"] == "story-mid", result
    assert result["resolved_target"] == "story-other", result
    assert result["was_redirected"] is True, result


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize(("type_a", "type_b"), [("task", "task"), ("task", "bug"), ("bug", "bug")])
def test_resolve_hierarchy_link_cousins_escalate_to_their_parents(
    graph: ModuleType, tmp_path: Path, type_a: str, type_b: str
) -> None:
    """Promote cousin endpoints to their parent stories regardless of ticket type."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    # separate parent stories
    _write_ticket(tracker_dir, "leaf-a", parent_id="story-a", ticket_type=type_a)
    _write_ticket(tracker_dir, "leaf-b", parent_id="story-b", ticket_type=type_b)

    result = graph.resolve_hierarchy_link("leaf-a", "leaf-b", str(tracker_dir), "depends_on")
    assert result["resolved_source"] == "story-a", result
    assert result["resolved_target"] == "story-b", result
    assert result["was_redirected"] is True, (
        f"({type_a},{type_b}): cousins should escalate to their parents, got {result!r}"
    )
    assert result["is_redundant"] is False, result


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_disjoint_trees_escalate_to_their_roots(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Resolve disjoint-tree links to each endpoint's own root.

    A parentless task and epic stay unchanged. A task under a root story redirects to
    that story. The virtual root supplies their common ancestor.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # Orphan task ↔ epic: no ancestor at all → resolves to itself, not redirected.
    _write_ticket(tracker_dir, "orphan-task", ticket_type="task")
    _write_ticket(tracker_dir, "the-epic", ticket_type="epic")
    res_orphan = graph.resolve_hierarchy_link("orphan-task", "the-epic", str(tracker_dir), "blocks")
    assert res_orphan["resolved_source"] == "orphan-task", res_orphan
    assert res_orphan["resolved_target"] == "the-epic", res_orphan
    assert res_orphan["was_redirected"] is False, res_orphan

    # Task under a story (no epic ancestor) ↔ epic: no epic-tier ancestor exists,
    # so fall back to the chain root (the story) and still flag the redirect.
    _write_ticket(tracker_dir, "lone-story", ticket_type="story")
    _write_ticket(tracker_dir, "story-task", parent_id="lone-story", ticket_type="task")
    res_fallback = graph.resolve_hierarchy_link(
        "story-task", "the-epic", str(tracker_dir), "blocks"
    )
    assert res_fallback["resolved_source"] == "lone-story", res_fallback
    assert res_fallback["resolved_target"] == "the-epic", res_fallback
    assert res_fallback["was_redirected"] is True, res_fallback


# Structural comparability covers redirected edges and ancestor refusal.


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_task_and_story_siblings_link_directly(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Link task and story siblings directly under their shared epic."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-e", ticket_type="epic")
    _write_ticket(tracker_dir, "story-s", parent_id="epic-e", ticket_type="story")
    _write_ticket(tracker_dir, "task-t1", parent_id="epic-e", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-t1", "story-s", str(tracker_dir), "depends_on")

    assert result["resolved_source"] == "task-t1", result
    assert result["resolved_target"] == "story-s", result
    assert result["was_redirected"] is False, result
    assert result["is_redundant"] is False, result
    assert "epic-e" not in (result["resolved_source"], result["resolved_target"]), (
        f"the epic must not become an endpoint of its own child's dependency, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_jira_reb_1582_worked_example(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Resolve Task D-to-Task B to Story C-to-Task B. Keep sibling B-to-C direct."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "task-b", parent_id="epic-a", ticket_type="task")
    _write_ticket(tracker_dir, "story-c", parent_id="epic-a", ticket_type="story")
    _write_ticket(tracker_dir, "task-d", parent_id="story-c", ticket_type="task")

    escalated = graph.resolve_hierarchy_link("task-d", "task-b", str(tracker_dir), "depends_on")
    assert escalated["resolved_source"] == "story-c", escalated
    assert escalated["resolved_target"] == "task-b", escalated
    assert escalated["was_redirected"] is True, escalated

    direct = graph.resolve_hierarchy_link("task-b", "story-c", str(tracker_dir), "depends_on")
    assert direct["resolved_source"] == "task-b", direct
    assert direct["resolved_target"] == "story-c", direct
    assert direct["was_redirected"] is False, direct
    assert direct["is_redundant"] is False, direct


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.parametrize("relation", ["blocks", "depends_on"])
def test_resolve_hierarchy_link_grandparent_pair_is_redundant(
    graph: ModuleType, tmp_path: Path, relation: str
) -> None:
    """Refuse blocking edges between any ancestor and descendant after resolution."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")

    downward = graph.resolve_hierarchy_link("epic-root", "task-leaf", str(tracker_dir), relation)
    assert downward["is_redundant"] is True, downward

    upward = graph.resolve_hierarchy_link("task-leaf", "epic-root", str(tracker_dir), relation)
    assert upward["is_redundant"] is True, upward


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_grandparent_pair_allowed_for_non_blocking(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Keep non-blocking grandparent links. Only direct parent-child is redundant."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-mid", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-leaf", parent_id="story-mid", ticket_type="task")

    result = graph.resolve_hierarchy_link("epic-root", "task-leaf", str(tracker_dir), "relates_to")

    assert result["resolved_source"] == "epic-root", result
    assert result["resolved_target"] == "task-leaf", result
    assert result["was_redirected"] is False, result
    assert result["is_redundant"] is False, result


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_deep_chain_escalates_to_nca_children(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Resolve deep cousin leaves to their stories below the nearest common epic."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b", parent_id="story-b", ticket_type="task")
    _write_ticket(tracker_dir, "sub-a", parent_id="task-a", ticket_type="task")
    _write_ticket(tracker_dir, "sub-b", parent_id="task-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("sub-a", "sub-b", str(tracker_dir), "depends_on")

    assert result["resolved_source"] == "story-a", result
    assert result["resolved_target"] == "story-b", result
    assert result["was_redirected"] is True, result


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_virtual_root_is_never_an_endpoint(
    graph: ModuleType, tmp_path: Path
) -> None:
    """The virtual-root sentinel is internal and must never be a resolved endpoint."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-a", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-a", ticket_type="story")
    _write_ticket(tracker_dir, "task-a", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "lonely-task", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a", "lonely-task", str(tracker_dir), "blocks")

    assert result["resolved_source"] == "epic-a", result
    assert result["resolved_target"] == "lonely-task", result
    for endpoint in (result["resolved_source"], result["resolved_target"]):
        assert (tracker_dir / str(endpoint)).is_dir(), (
            f"resolved endpoint {endpoint!r} is not a real ticket directory"
        )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_parent_cycle_terminates(graph: ModuleType, tmp_path: Path) -> None:
    """A malformed parent cycle must terminate the ancestor walk, not hang.

    The chain walk is unbounded now that the two-hop cap is gone, so a store whose
    parent pointers form a loop would spin forever without the visited-set guard.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    # cycle-a -> cycle-b -> cycle-a
    _write_ticket(tracker_dir, "cycle-a", parent_id="cycle-b", ticket_type="task")
    _write_ticket(tracker_dir, "cycle-b", parent_id="cycle-a", ticket_type="task")
    _write_ticket(tracker_dir, "unrelated", ticket_type="task")

    result = graph.resolve_hierarchy_link("cycle-a", "unrelated", str(tracker_dir), "blocks")

    assert "resolved_source" in result, result
    assert "resolved_target" in result, result
