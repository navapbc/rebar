"""Boundary test: an unresolvable / archived `depends_on` target is treated as a
CLOSED blocker (tombstone-awareness), so the dependent IS ready.

Story lean-sloth-ham (epic clumsy-jab-yacht) investigated the audit's H-5 claim that
an unresolvable-alias blocker lets a blocked ticket "wrongly" read as ready. On
inspection that is the DELIBERATE tombstone invariant: a blocker whose directory is
absent (archived / deleted / not-yet-synced) must not block its dependents forever
(see _status._get_ticket_status and test_graph_traversal's archived-blocker test).
`resolve_ticket_id` returns None for BOTH normal archival and a genuinely-bogus
alias, indistinguishably, so failing closed on "unresolved" would break archived-
blocker unblocking. These tests codify the boundary so it is not re-flagged, and the
controls prove normal blocking still works.
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
    """ticket-x depends_on a target whose dir is absent → x IS ready (tombstone).

    This is the deliberate invariant, NOT a bug: an archived/deleted/not-yet-synced
    blocker must not block forever. Documented here so future audits don't re-flag it.
    """
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
