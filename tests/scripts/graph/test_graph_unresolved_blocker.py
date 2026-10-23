"""Treat unresolved depends_on targets as satisfied tombstones.

Alias resolution cannot distinguish archived or deleted targets from a bogus alias.
Normal resolvable blockers still block until terminal.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _helpers import _write_link_event, _write_ticket


def _ready_ids(tracker_dir: Path) -> set[str]:
    from rebar.graph._ready import find_ready_tickets

    return {s.get("ticket_id") for s in find_ready_tickets(str(tracker_dir))}


@pytest.mark.unit
@pytest.mark.scripts
def test_depends_on_absent_target_is_treated_as_closed_tombstone(
    graph: ModuleType, tmp_path: Path
) -> None:
    """An absent depends_on target cannot block a dependent forever."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, "ticket-x", status="open")
    _write_link_event("ticket-x", "no-such-ticket-alias", "depends_on", str(tracker))

    assert "ticket-x" in _ready_ids(tracker)
    # build_dep_graph agrees (the deps-display readiness path).
    assert graph.build_dep_graph("ticket-x", str(tracker))["ready_to_work"] is True


@pytest.mark.unit
@pytest.mark.scripts
def test_depends_on_resolvable_open_target_blocks_normally(
    graph: ModuleType, tmp_path: Path
) -> None:
    """Control: depends_on a real OPEN ticket → blocked (not ready)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, "ticket-x", status="open")
    _write_ticket(tracker, "ticket-y", status="open")
    _write_link_event("ticket-x", "ticket-y", "depends_on", str(tracker))

    assert "ticket-x" not in _ready_ids(tracker)  # blocked by open ticket-y


@pytest.mark.unit
@pytest.mark.scripts
def test_depends_on_resolvable_closed_target_is_ready(graph: ModuleType, tmp_path: Path) -> None:
    """Control: depends_on a real CLOSED ticket → ready."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _write_ticket(tracker, "ticket-x", status="open")
    _write_ticket(tracker, "ticket-y", status="closed")
    _write_link_event("ticket-x", "ticket-y", "depends_on", str(tracker))

    assert "ticket-x" in _ready_ids(tracker)
