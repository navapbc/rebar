"""resolve_hierarchy_link promotion (SC1/3/5/10/11, type-tier comparability)

Split from the former monolithic tests/scripts/test_ticket_graph.py along
graph-concern seams. The `graph` fixture + autouse git-isolation fixture live in
conftest.py; event-writing helpers + the module loader in _helpers.py.
"""

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
    """SC1: Two tasks sharing the same parent story → original IDs unchanged.

    Setup:
        - story-parent: story (no parent)
        - task-a: task with parent_id=story-parent
        - task-b: task with parent_id=story-parent

    Expected: resolved_source=task-a, resolved_target=task-b,
              was_redirected=False, is_redundant=False
    """
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
    """SC3 (new type-tier semantics): cross-story SAME-tier task pair → NOT promoted.

    Under the type-tier model, both endpoints are tasks (tier 0 == tier 0), so a
    blocking dep links them directly. They are NOT promoted to their parent
    stories — promotion only kicks in when the endpoints differ in tier.

    Setup:
        - epic-root: epic (no parent)
        - story-a: story with parent_id=epic-root
        - story-b: story with parent_id=epic-root
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=task-a1, resolved_target=task-b1,
              was_redirected=False, is_redundant=False
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "task-a1", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker_dir, "task-b1", parent_id="story-b", ticket_type="task")

    result = graph.resolve_hierarchy_link("task-a1", "task-b1", str(tracker_dir), "blocks")

    assert result["resolved_source"] == "task-a1", (
        f"SC3: expected resolved_source='task-a1', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b1", (
        f"SC3: expected resolved_target='task-b1', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC3: expected was_redirected=False (same tier), got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC3: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_cross_epic_sc5(graph: ModuleType, tmp_path: Path) -> None:
    """SC5 (new type-tier semantics): cross-epic SAME-tier task pair → NOT promoted.

    Both endpoints are tasks (tier 0). Even across separate epics, a same-tier
    blocking dep links the exact tasks; promotion only applies across tiers.

    Setup:
        - epic-a: epic (no parent)
        - epic-b: epic (no parent)
        - story-a: story with parent_id=epic-a
        - story-b: story with parent_id=epic-b
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=task-a1, resolved_target=task-b1,
              was_redirected=False, is_redundant=False
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

    assert result["resolved_source"] == "task-a1", (
        f"SC5: expected resolved_source='task-a1', got {result['resolved_source']!r}"
    )
    assert result["resolved_target"] == "task-b1", (
        f"SC5: expected resolved_target='task-b1', got {result['resolved_target']!r}"
    )
    assert result["was_redirected"] is False, (
        f"SC5: expected was_redirected=False (same tier), got {result['was_redirected']!r}"
    )
    assert result["is_redundant"] is False, (
        f"SC5: expected is_redundant=False, got {result['is_redundant']!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_orphan_ticket_sc10(graph: ModuleType, tmp_path: Path) -> None:
    """SC10: Tickets with no parent_id → original IDs unchanged.

    Setup:
        - orphan-a: task (no parent)
        - orphan-b: task (no parent)

    Expected: resolved_source=orphan-a, resolved_target=orphan-b,
              was_redirected=False, is_redundant=False
    """
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
    """SC11: If ticket state cannot be reduced → AttributeError or returns error dict.

    Setup:
        - ticket-ok: valid task
        - missing-ticket: does not exist in tracker

    Expected: resolve_hierarchy_link returns a dict with 'error' key (not silent fallthrough)
    """
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
    """is_redundant=True when source IS the direct parent of target.

    Setup:
        - story-parent: story (no parent)
        - task-child: task with parent_id=story-parent

    Expected: resolved_source=story-parent, resolved_target=task-child (or vice versa),
              is_redundant=True (because story-parent is the direct parent of task-child)
    """
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
    """(a) Non-blocking relations are NEVER promoted — exact pair, was_redirected=False.

    Even with a maximal tier gap (task ↔ epic), a non-blocking relation links the
    exact source/target the caller passed.
    """
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
    """(b) blocks/depends_on between a task and an epic promotes the task to its epic.

    task-leaf (tier 0) under story-mid under epic-root, linked to epic-other
    (tier 2). The lower endpoint (task) is promoted up its chain to its epic
    ancestor → epic-root ↔ epic-other.
    """
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
@pytest.mark.parametrize("type_a,type_b", [("task", "task"), ("task", "bug"), ("bug", "bug")])
def test_resolve_hierarchy_link_same_tier_siblings_cousins_not_promoted(
    graph: ModuleType, tmp_path: Path, type_a: str, type_b: str
) -> None:
    """(c) Same-tier (task/bug) blocking link between siblings/cousins is NOT promoted.

    task and bug are both leaf tier 0, so they are comparable and link directly —
    regardless of whether they are siblings (same story) or cousins (different
    stories). Also exercises the bug==task tier mapping.
    """
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()

    _write_ticket(tracker_dir, "epic-root", ticket_type="epic")
    _write_ticket(tracker_dir, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker_dir, "story-b", parent_id="epic-root", ticket_type="story")
    # cousins: different parent stories
    _write_ticket(tracker_dir, "leaf-a", parent_id="story-a", ticket_type=type_a)
    _write_ticket(tracker_dir, "leaf-b", parent_id="story-b", ticket_type=type_b)

    result = graph.resolve_hierarchy_link("leaf-a", "leaf-b", str(tracker_dir), "depends_on")
    assert result["resolved_source"] == "leaf-a", result
    assert result["resolved_target"] == "leaf-b", result
    assert result["was_redirected"] is False, (
        f"({type_a},{type_b}): same leaf tier should not promote, got {result!r}"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_fallback_no_comparable_ancestor(
    graph: ModuleType, tmp_path: Path
) -> None:
    """(d) Fallback: no comparable-tier ancestor → highest ancestor (chain root), still redirected.

    An orphan task (no parent) blocking an epic has NO epic ancestor to promote
    to. Per the fallback rule it promotes to the chain root — which for an orphan
    is itself — so was_redirected stays False (nothing moved).

    The richer fallback: a task whose only ancestor is a STORY (no epic above it)
    linked to an EPIC. There is no epic-tier ancestor, so it falls back to the
    chain root (the story) and reports was_redirected=True.
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
