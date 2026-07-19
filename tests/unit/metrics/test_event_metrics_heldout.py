"""Held-out contracts for the agent-process readers (ticket 18e6). WITHHELD.

- rework_within_days: a close->reopen->close within N days counts; a wider gap does not,
- revert_recovery: counts a ticket whose revert targets a STATUS event, NOT one whose
  only revert targets a COMMENT (the substantive-revert filter),
- None (→ Unavailable) on an empty range vs a measured 0 on a populated range,
- retired (*.retired) events are still counted (compaction survival),
- the metrics register into the c085 registry with source="structural" (authoritative).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.event_metrics import attempts_per_ticket, revert_recovery, rework_within_days
from rebar.metrics.registry import REGISTRY, is_authoritative

pytestmark = pytest.mark.unit

_ENV = "eeee-0000-4000-8000-000000000001"


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _write_event(
    ticket_dir: Path, ts_ns: int, event_type: str, data: dict, retired: bool = False
) -> None:
    ev_uuid = str(uuid.uuid4())
    ev = {
        "event_type": event_type,
        "timestamp": ts_ns,
        "uuid": ev_uuid,
        "env_id": _ENV,
        "author": "t",
        "data": data,
    }
    suffix = ".retired" if retired else ""
    (ticket_dir / f"{ts_ns:020d}-{ev_uuid}-{event_type}.json{suffix}").write_text(
        json.dumps(ev), encoding="utf-8"
    )


def _ticket(tracker: Path, tid: str, ttype: str = "task") -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event(d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": ttype, "title": tid})
    return d


def _status(d: Path, ts_iso: str, cur: str, new: str, **extra) -> None:
    _write_event(d, _ns(ts_iso), "STATUS", {"status": new, "current_status": cur, **extra})


def test_rework_within_days_window(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # close -> reopen -> close, 3 days between reopen and re-close: within 7, not <2.
    d = _ticket(tracker, "cccc-0000-0000-0003")
    _status(d, "2026-02-01T00:00:00", "in_progress", "closed")
    _status(d, "2026-02-02T00:00:00", "closed", "open")
    _status(d, "2026-02-05T00:00:00", "in_progress", "closed")

    assert rework_within_days(str(tmp_path), 7) >= 1  # 3-day rework counts within 7
    assert rework_within_days(str(tmp_path), 2) == 0  # ...but not within a 2-day window


def test_revert_recovery_substantive_only(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # Ticket A: a REVERT targeting a STATUS event -> counts.
    a = _ticket(tracker, "dddd-0000-0000-0004")
    _write_event(
        a,
        _ns("2026-02-03T00:00:00"),
        "REVERT",
        {"target_event_uuid": "x", "target_event_type": "STATUS", "reason": "r"},
    )
    # Ticket B: a REVERT targeting only a COMMENT -> does NOT count.
    b = _ticket(tracker, "eeee-0000-0000-0005")
    _write_event(
        b,
        _ns("2026-02-03T00:00:00"),
        "REVERT",
        {"target_event_uuid": "y", "target_event_type": "COMMENT", "reason": "r"},
    )

    n = revert_recovery(str(tmp_path))
    assert n == 1  # only the STATUS-targeting revert is substantive


def test_none_on_empty_range_but_zero_when_populated(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # An empty store: no qualifying tickets at all -> None (=> Unavailable).
    assert rework_within_days(str(tmp_path), 7, since="2026-01-01", until="2026-12-31") is None

    # A populated store with a ticket that has NO rework -> measured 0, not None.
    d = _ticket(tracker, "ffff-0000-0000-0006")
    _status(d, "2026-02-01T00:00:00", "in_progress", "closed")  # closed once, never reopened
    assert rework_within_days(str(tmp_path), 7, since="2026-01-01", until="2026-12-31") == 0


def test_retired_events_still_counted(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "0007-0000-0000-0007")
    # A retired (compaction-folded) claim event must still be counted.
    _write_event(
        d,
        _ns("2026-02-01T00:00:00"),
        "STATUS",
        {"status": "in_progress", "current_status": "open", "session": "sess-R"},
        retired=True,
    )
    result = attempts_per_ticket(str(tmp_path))
    assert result.get("0007-0000-0000-0007") == 1


def test_event_metrics_registered_authoritative():
    struct = [s for s in REGISTRY if s.source == "structural"]
    assert struct, "18e6 must register at least one structural agent-process metric"
    for s in struct:
        assert is_authoritative(s.source) is True
    # at least one agent-process metric id is registered
    agent_ids = {s.id for s in REGISTRY if s.lens in ("agent_process", "agent-process")}
    assert agent_ids, "18e6 must register agent_process-lens metrics"
