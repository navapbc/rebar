"""Held-out reducer transition contract for tri-state FILE_IMPACT."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, _write_event

_UUID4 = "11111111-2222-4333-8444-555555555555"


@pytest.mark.unit
@pytest.mark.scripts
def test_file_impact_scope_transition_table(
    tmp_path: Path,
    reducer: ModuleType,
) -> None:
    ticket_dir = tmp_path / "tkt-file-impact-scope"
    ticket_dir.mkdir()
    timestamp = 1_700_100_000

    _write_event(
        ticket_dir,
        timestamp=timestamp,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "title": "Tri-state",
            "ticket_type": "task",
            "author": "test",
            "priority": 2,
        },
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["file_impact_scope"] == "undeclared"
    assert state["no_file_impact_reason"] == ""
    assert state["file_impact"] == []

    first = [{"path": "src/first.py", "reason": "implementation"}]
    _write_event(
        ticket_dir,
        timestamp=timestamp + 1,
        uuid=_UUID2,
        event_type="FILE_IMPACT",
        data={"file_impact": first},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["file_impact_scope"] == "paths"
    assert state["no_file_impact_reason"] == ""
    assert state["file_impact"] == first

    reason = "operator action only"
    _write_event(
        ticket_dir,
        timestamp=timestamp + 2,
        uuid=_UUID3,
        event_type="FILE_IMPACT",
        data={
            "file_impact": [],
            "file_impact_scope": "none",
            "no_file_impact_reason": reason,
        },
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["file_impact_scope"] == "none"
    assert state["no_file_impact_reason"] == reason
    assert state["file_impact"] == []

    second = [{"path": "src/second.py", "reason": "implementation"}]
    _write_event(
        ticket_dir,
        timestamp=timestamp + 3,
        uuid=_UUID4,
        event_type="FILE_IMPACT",
        data={"file_impact": second},
    )
    state = reducer.reduce_ticket(ticket_dir)
    assert state["file_impact_scope"] == "paths"
    assert state["no_file_impact_reason"] == ""
    assert state["file_impact"] == second


@pytest.mark.unit
@pytest.mark.scripts
def test_legacy_snapshot_derives_paths_scope(
    tmp_path: Path,
    reducer: ModuleType,
) -> None:
    ticket_dir = tmp_path / "tkt-file-impact-scope-snapshot"
    ticket_dir.mkdir()
    legacy_paths = [{"path": "src/legacy.py", "reason": "pre-feature snapshot"}]

    _write_event(
        ticket_dir,
        timestamp=1_700_200_000,
        uuid=_UUID,
        event_type="SNAPSHOT",
        data={
            "compiled_state": {
                "ticket_id": "tkt-file-impact-scope-snapshot",
                "ticket_type": "task",
                "title": "Legacy compacted ticket",
                "status": "open",
                "file_impact": legacy_paths,
            }
        },
    )

    state = reducer.reduce_ticket(ticket_dir)
    assert state["file_impact_scope"] == "paths"
    assert state["no_file_impact_reason"] == ""
    assert state["file_impact"] == legacy_paths
