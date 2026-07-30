"""``link-audit`` — classification, repair ordering, and the fail-safe paths.

The command finds blocking edges that predate the structural link rule (ticket
7ab3-9df0-7a90-4ffd) and optionally repairs them. The properties worth pinning are
less about the happy path than about what happens when a repair CANNOT complete:
no failure path may lose a dependency, and a pair that cannot be unlinked
relation-precisely must be declined rather than guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from _helpers import (
    _write_ticket,
)


def _tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    return tracker


def _event_count(tracker: Path) -> int:
    return len(list(tracker.glob("*/*.json")))


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_reports_nothing_for_a_clean_store(graph: ModuleType, tmp_path: Path) -> None:
    """Sibling edges agree with the resolver, so nothing is reported."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "story-parent", ticket_type="story")
    _write_ticket(tracker, "task-a", parent_id="story-parent", ticket_type="task")
    _write_ticket(tracker, "task-b", parent_id="story-parent", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_reports_nothing_when_there_are_no_blocking_edges(
    graph: ModuleType, tmp_path: Path
) -> None:
    """An empty dependency graph is clean, not an error."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "lonely", ticket_type="task")

    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_an_epic_blocked_by_its_own_child(
    graph: ModuleType, tmp_path: Path
) -> None:
    """The bug 1803 shape on disk: an epic depending on its own child."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")

    findings = link_audit.scan(str(tracker))

    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "ancestor-blocking", findings[0]
    assert findings[0]["source"] == "epic-e"
    assert findings[0]["target"] == "story-s"


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_a_cousin_edge_as_mis_escalated(graph: ModuleType, tmp_path: Path) -> None:
    """A cousin edge recorded under the old rule now resolves to the parents."""
    from rebar._commands import link_audit

    tracker = _cousin_store(tmp_path)
    findings = link_audit.scan(str(tracker))

    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "mis-escalated", findings[0]
    assert findings[0]["resolved_source"] == "story-a"
    assert findings[0]["resolved_target"] == "story-b"


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_ignores_non_blocking_relations(graph: ModuleType, tmp_path: Path) -> None:
    """Only blocks/depends_on are audited; the soft relations are never touched."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    # An ancestor pair that WOULD be flagged were the relation blocking. Seeded raw:
    # add_dependency refuses a direct parent-child pair for ANY relation, because
    # is_redundant is computed from the original pair before the non-blocking return.
    _seed_link(tracker, "epic-e", "story-s", "relates_to")

    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_scan_classifies_an_unreadable_endpoint(graph: ModuleType, tmp_path: Path) -> None:
    """An edge pointing at a ticket that no longer exists is reported, not raised."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "task-a", ticket_type="task")
    _write_ticket(tracker, "task-b", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    import shutil

    shutil.rmtree(tracker / "task-b")

    findings = link_audit.scan(str(tracker))
    assert len(findings) == 1, findings
    assert findings[0]["kind"] == "unreadable", findings[0]


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_removes_an_ancestor_blocking_edge(graph: ModuleType, tmp_path: Path) -> None:
    """Repair unlinks the bad edge, and a second scan comes back clean."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")

    findings = link_audit.scan(str(tracker))
    link_audit.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "repaired", findings[0]
    assert not graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker))
    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_replaces_a_mis_escalated_edge(graph: ModuleType, tmp_path: Path) -> None:
    """Repair writes the resolved pair and removes the stale one."""
    from rebar._commands import link_audit

    tracker = _cousin_store(tmp_path)
    findings = link_audit.scan(str(tracker))
    link_audit.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "repaired", findings[0]
    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker))
    assert not graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker))
    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_writes_the_replacement_before_removing_the_stale_edge(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering invariant: a failed unlink must never cost us the dependency.

    Unlink-first would leave nothing on disk to reconstruct the edge from. With
    link-first, the same failure leaves a SUPERSET — both edges present — which the
    next scan converges. This forces the unlink to raise and asserts nothing is lost.
    """
    from rebar._commands import link_audit

    tracker = _cousin_store(tmp_path)
    findings = link_audit.scan(str(tracker))

    def _boom(*_a, **_k):
        raise ValueError("unlink exploded")

    monkeypatch.setattr(link_audit, "_unlink_edge", _boom)
    link_audit.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker)), (
        "the replacement must already be durable when the unlink fails"
    )
    assert graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker)), (
        "the original edge must survive a failed unlink — no dependency is lost"
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_is_resumable_from_the_superset_state(
    graph: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running repair over the interrupted superset converges to the replacement."""
    from rebar._commands import link_audit

    tracker = _cousin_store(tmp_path)

    def _boom(*_a, **_k):
        raise ValueError("unlink exploded")

    original_unlink = link_audit._unlink_edge
    monkeypatch.setattr(link_audit, "_unlink_edge", _boom)
    link_audit.repair_finding(link_audit.scan(str(tracker))[0], str(tracker))

    # Restore by re-patching, NOT via monkeypatch.undo(): undo() reverts every patch
    # made through this fixture instance — including the git isolation conftest
    # installs — which would point the store at an uninitialized location.
    monkeypatch.setattr(link_audit, "_unlink_edge", original_unlink)
    for finding in link_audit.scan(str(tracker)):
        link_audit.repair_finding(finding, str(tracker))

    assert graph._is_active_link("story-a", "story-b", "depends_on", str(tracker))
    assert not graph._is_active_link("leaf-a", "leaf-b", "depends_on", str(tracker))
    assert link_audit.scan(str(tracker)) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_declines_a_pair_whose_unlink_would_cancel_another_relation(
    graph: ModuleType, tmp_path: Path
) -> None:
    """UNLINK is pair-scoped, so an ambiguous pair is declined, not guessed at.

    The pair carries a blocking edge AND a newer relates_to. Unlinking would cancel
    the relates_to, so repair must refuse and leave both links intact.
    """
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "epic-e", ticket_type="epic")
    _write_ticket(tracker, "story-s", parent_id="epic-e", ticket_type="story")
    _seed_link(tracker, "epic-e", "story-s", "depends_on")
    _seed_link(tracker, "epic-e", "story-s", "relates_to", suffix="2")

    findings = [f for f in link_audit.scan(str(tracker)) if f["kind"] == "ancestor-blocking"]
    assert findings, "the blocking edge should still be classified"
    link_audit.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert "ambiguous-pair" in findings[0]["repair_reason"], findings[0]
    assert graph._is_active_link("epic-e", "story-s", "depends_on", str(tracker))
    assert graph._is_active_link("epic-e", "story-s", "relates_to", str(tracker))


@pytest.mark.unit
@pytest.mark.scripts
def test_repair_never_touches_an_unreadable_finding(graph: ModuleType, tmp_path: Path) -> None:
    """An unreadable edge is reported and left exactly as it was."""
    from rebar._commands import link_audit

    tracker = _tracker(tmp_path)
    _write_ticket(tracker, "task-a", ticket_type="task")
    _write_ticket(tracker, "task-b", ticket_type="task")
    graph.add_dependency("task-a", "task-b", str(tracker), relation="depends_on")

    import shutil

    shutil.rmtree(tracker / "task-b")

    findings = link_audit.scan(str(tracker))
    before = _event_count(tracker)
    link_audit.repair_finding(findings[0], str(tracker))

    assert findings[0]["repair_status"] == "unrepairable", findings[0]
    assert findings[0]["repair_reason"] == "unreadable-endpoint"
    assert _event_count(tracker) == before, "an unreadable finding must write nothing"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _cousin_store(tmp_path: Path) -> Path:
    """Two leaves in different stories under one epic, linked directly.

    That direct edge is exactly what the old type-tier rule produced (both leaves
    were tier 0, so nothing escalated) and what the structural rule now resolves to
    the parent stories.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir(exist_ok=True)
    _write_ticket(tracker, "epic-root", ticket_type="epic")
    _write_ticket(tracker, "story-a", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker, "story-b", parent_id="epic-root", ticket_type="story")
    _write_ticket(tracker, "leaf-a", parent_id="story-a", ticket_type="task")
    _write_ticket(tracker, "leaf-b", parent_id="story-b", ticket_type="task")
    _seed_link(tracker, "leaf-a", "leaf-b", "depends_on")
    return tracker


def _seed_link(tracker: Path, source: str, target: str, relation: str, suffix: str = "1") -> None:
    """Write a raw LINK event, bypassing add_dependency's guards.

    The whole point of this command is edges the CURRENT rules would refuse, so the
    fixtures cannot be built through ``add_dependency`` — it rejects exactly these
    shapes. Writing the event directly is what a store predating the rule looks like.
    """
    event = {
        "event_type": "LINK",
        "uuid": f"link-{source}-{target}-{suffix}",
        "timestamp": 2000 + int(suffix),
        "author": "Test User",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "data": {"target_id": target, "relation": relation},
    }
    path = tracker / source / f"{2000 + int(suffix)}-link-{source}-{target}-{suffix}-LINK.json"
    path.write_text(json.dumps(event), encoding="utf-8")
