"""7657 (epic 7738): reduce_all_tickets(exclude_session_logs=...) flag semantics.

The flag is the single seam the graph/health hot paths (`ready`, `next_batch`,
`deps`, `validate`) and default `list` set to keep verbose `session_log` bodies
out of the compile that backs them, mirroring the existing exclude_archived /
exclude_deleted post-filters. Default OFF (so `search` / `show` keep logs).
Error dicts (no ``ticket_type``) must be preserved intact, exactly as the
exclude_deleted filter preserves error dicts (no ``status``).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _write_event


def _write_ticket(tracker_dir: Path, name: str, ticket_type: str, title: str) -> None:
    d = tracker_dir / name
    d.mkdir()
    _write_event(
        d,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": ticket_type, "title": title},
    )


@pytest.mark.unit
@pytest.mark.scripts
def test_exclude_session_logs_drops_only_logs(tmp_path: Path, reducer: ModuleType) -> None:
    """exclude_session_logs=True drops session_log tickets and keeps the rest."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()
    _write_ticket(tracker_dir, "tkt-task", "task", "a task")
    _write_ticket(tracker_dir, "tkt-epic", "epic", "an epic")
    _write_ticket(tracker_dir, "tkt-log", "session_log", "a log")

    types_off = {r.get("ticket_type") for r in reducer.reduce_all_tickets(str(tracker_dir))}
    assert "session_log" in types_off, "default OFF must keep session_log (search/show path)"

    excluded = reducer.reduce_all_tickets(str(tracker_dir), exclude_session_logs=True)
    types_on = {r.get("ticket_type") for r in excluded}
    assert "session_log" not in types_on
    assert {"task", "epic"} <= types_on, "non-log tickets must survive the exclusion"


@pytest.mark.unit
@pytest.mark.scripts
def test_exclude_session_logs_preserves_error_dicts(tmp_path: Path, reducer: ModuleType) -> None:
    """Error dicts (no ``ticket_type``) must pass through the filter intact."""
    tracker_dir = tmp_path / "tracker"
    tracker_dir.mkdir()
    _write_ticket(tracker_dir, "tkt-log", "session_log", "a log")

    # A ticket dir whose first event is not a CREATE reduces to an error/empty
    # state whose ``ticket_type`` is None — the filter keys on
    # ``r.get("ticket_type") != "session_log"`` so None (≠ "session_log") is kept,
    # exactly as exclude_deleted preserves dicts whose ``status`` is absent.
    broken = tracker_dir / "tkt-broken"
    broken.mkdir()
    _write_event(
        broken,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="COMMENT",
        data={"body": "orphan comment, no CREATE"},
    )

    excluded = reducer.reduce_all_tickets(str(tracker_dir), exclude_session_logs=True)
    error_dicts = [r for r in excluded if r.get("ticket_type") is None]
    assert any(r.get("ticket_id") == "tkt-broken" for r in error_dicts), (
        "an error/empty dict (ticket_type=None) must survive exclude_session_logs"
    )
    assert all(r.get("ticket_type") != "session_log" for r in excluded)
