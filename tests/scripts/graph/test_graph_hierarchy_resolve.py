"""resolve_hierarchy_link escalation (SC1/3/5/10/11, structural comparability)

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
    """SC3 (structural semantics): cross-story task pair → escalated to the stories.

    The endpoints do not share a parent, so they are not comparable as given. Their
    nearest common ancestor is epic-root, and each escalates to its own ancestor
    directly below it — the parent stories. Ticket type plays no part: this held
    the opposite expectation under the former type-tier rule, where both endpoints
    were leaf tier and therefore linked directly.

    Setup:
        - epic-root: epic (no parent)
        - story-a: story with parent_id=epic-root
        - story-b: story with parent_id=epic-root
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=story-a, resolved_target=story-b,
              was_redirected=True, is_redundant=False
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
    """SC5 (structural semantics): cross-epic task pair → escalated to the epics.

    The two tasks live in different trees. Both epics are parentless, and parentless
    tickets count as siblings of one another, so the nearest common ancestor is the
    virtual root and each endpoint escalates to its own real root. This held the
    opposite expectation under the former type-tier rule, where both endpoints were
    leaf tier and therefore linked directly.

    Setup:
        - epic-a: epic (no parent)
        - epic-b: epic (no parent)
        - story-a: story with parent_id=epic-a
        - story-b: story with parent_id=epic-b
        - task-a1: task with parent_id=story-a
        - task-b1: task with parent_id=story-b

    Expected: resolved_source=epic-a, resolved_target=epic-b,
              was_redirected=True, is_redundant=False
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
@pytest.mark.parametrize(("type_a", "type_b"), [("task", "task"), ("task", "bug"), ("bug", "bug")])
def test_resolve_hierarchy_link_cousins_escalate_to_their_parents(
    graph: ModuleType, tmp_path: Path, type_a: str, type_b: str
) -> None:
    """(c) Cousins do NOT share a parent, so a blocking link escalates to their parents.

    Comparability is structural, so ticket type is irrelevant here: task/task,
    task/bug and bug/bug all behave identically. The pair's nearest common ancestor
    is epic-root, so each endpoint escalates to its own parent story.

    This inverts the former type-tier expectation, under which task and bug were
    both leaf tier and cousins therefore linked directly.
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
    """(d) Across disjoint trees, each endpoint escalates to its own root.

    Parentless tickets are siblings of one another, so the nearest common ancestor
    of two disjoint trees is the virtual root and each endpoint resolves to the
    real root beneath it.

    An orphan task blocking an epic is therefore already at that level: both are
    roots, so nothing moves and was_redirected stays False.

    A task whose only ancestor is a STORY, linked to an unrelated EPIC, escalates
    to that story — the task's own root — and reports was_redirected=True.

    These assertions are unchanged from when this test covered the type-tier
    fallback; only the name and rationale change, since the structural rule
    produces the same endpoints by a different route.
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


# ---------------------------------------------------------------------------
# Structural comparability (story affe-2b42-4ee4-4e12): the rule replacing the
# former type-tier promotion. Regression cover for bugs 1803-df54-18bb-4881 and
# jira-reb-1582.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_resolve_hierarchy_link_task_and_story_siblings_link_directly(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Bug 1803 repro: a task and a story that share a parent epic link as given.

    Under the former type-tier rule the task was promoted toward the story's tier,
    found no story ancestor, and fell back to the chain root — the epic — so the
    store recorded the epic depending on its own child and the requested edge was
    never written. Structurally the two ARE siblings, so nothing escalates.
    """
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
    """The worked example from bug jira-reb-1582, asserted in both directions.

    Epic A parents Task B and Story C; Story C parents Task D. A dependency
    between Task D and Task B escalates to Story C and Task B, while a dependency
    between Task B and Story C — already siblings — is recorded directly.
    """
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
    """An ancestor/descendant blocking pair is refused, not just a direct parent.

    The guard is evaluated on the RESOLVED pair, so it covers a grandparent and
    grandchild — two hops apart — as well as the direct parent-child case that the
    pre-existing guard already caught.
    """
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
    """The widened blocking predicate must not leak into non-blocking relations.

    `is_redundant` for a non-blocking relation keeps its original meaning — the
    direct parent-child case only — so a grandparent/grandchild `relates_to` is
    still recorded exactly as given.
    """
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
    """A chain deeper than two hops resolves against the NCA, not the chain root.

    The former ancestor walk stopped after two hops, so a four-level chain could
    not see its own root and resolution fell back to whatever it had reached. Here
    the two leaves sit four levels down in different sub-trees whose nearest common
    ancestor is the epic, so each escalates to its own story — NOT to the epic.
    """
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
