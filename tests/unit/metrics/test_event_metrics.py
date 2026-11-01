"""Contract tests for ``attempts_per_ticket`` in ``rebar.metrics.event_metrics``.

Raw events supply claim-session identity absent from compiled state. The reader counts distinct
sessions from ``open->in_progress`` status events within the requested range. Its result maps
each ticket ID to an attempt count.

Held-out oracles merged from test_event_metrics_heldout: Held-out contracts for the agent-
process readers (ticket 18e6). WITHHELD.
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
    ticket_dir: Path, ts_ns: int, event_type: str, data: dict, author: str = "t"
) -> None:
    ev_uuid = str(uuid.uuid4())
    ev = {
        "event_type": event_type,
        "timestamp": ts_ns,
        "uuid": ev_uuid,
        "env_id": _ENV,
        "author": author,
        "data": data,
    }
    (ticket_dir / f"{ts_ns:020d}-{ev_uuid}-{event_type}.json").write_text(
        json.dumps(ev), encoding="utf-8"
    )


def _ticket(tracker: Path, tid: str, ttype: str = "task", title: str | None = None) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event(
        d,
        _ns("2026-01-01T00:00:00"),
        "CREATE",
        {"ticket_type": ttype, "title": title or tid},
    )
    return d


def _claim(ticket_dir: Path, ts_iso: str, session: str) -> None:
    _write_event(
        ticket_dir,
        _ns(ts_iso),
        "STATUS",
        {"status": "in_progress", "current_status": "open", "session": session},
    )


def _set_file_impact(ticket_dir: Path, path: str) -> None:
    _write_event(
        ticket_dir,
        _ns("2026-02-01T00:00:30"),
        "FILE_IMPACT",
        {"file_impact": [{"path": path, "reason": "test"}]},
    )


def test_attempts_counts_distinct_claim_sessions(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "aaaa-0000-0000-0001")
    # Two distinct claim sessions (open->in_progress twice, different session ids).
    _claim(d, "2026-02-01T00:00:00", "sess-A")
    _claim(d, "2026-02-05T00:00:00", "sess-B")

    result = attempts_per_ticket(str(tmp_path))
    assert result["aaaa-0000-0000-0001"] == 2


def test_single_session_is_one_attempt(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "bbbb-0000-0000-0002")
    _claim(d, "2026-02-01T00:00:00", "sess-only")

    result = attempts_per_ticket(str(tmp_path))
    assert result["bbbb-0000-0000-0002"] == 1


def test_duplicate_title_peak_hour_uses_create_warning_normalization(tmp_path):
    from rebar._commands.recent_creates import normalize_title
    from rebar.metrics.event_metrics import duplicate_title_peak_hour

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for index, title in enumerate(("Fix THE thing!", " fix   the thing ", "Other work")):
        d = _ticket(tracker, f"dddd-0000-0000-000{index}", title=title)
        create = next(d.glob("*-CREATE.json"))
        event = json.loads(create.read_text(encoding="utf-8"))
        event["timestamp"] = _ns(f"2026-02-01T00:0{index}:00")
        event["data"]["title"] = title
        create.write_text(json.dumps(event), encoding="utf-8")

    assert normalize_title("Fix THE thing!") == normalize_title(" fix   the thing ")
    result = duplicate_title_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00")

    assert result == {"peak_hour": 1, "alarm_threshold": 10, "alarm": False}


def test_duplicate_title_peak_hour_alarms_on_mass_duplicates(tmp_path):
    from rebar.metrics.event_metrics import duplicate_title_peak_hour

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for index in range(12):
        d = _ticket(tracker, f"mass-0000-0000-{index:04d}", title="Same title")
        create = next(d.glob("*-CREATE.json"))
        event = json.loads(create.read_text(encoding="utf-8"))
        event["timestamp"] = _ns(f"2026-02-01T00:{index:02d}:00")
        create.write_text(json.dumps(event), encoding="utf-8")

    result = duplicate_title_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00")

    assert result == {"peak_hour": 11, "alarm_threshold": 10, "alarm": True}


def test_duplicate_title_peak_hour_suppresses_distinct_file_impact(tmp_path):
    from rebar.metrics.event_metrics import duplicate_title_peak_hour

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for index, path in enumerate(("src/pkg/__init__.py", "src/pkg/_init.py")):
        d = _ticket(tracker, f"path-0000-0000-000{index}", title="Condense comments in init py")
        _set_file_impact(d, path)
        create = next(d.glob("*-CREATE.json"))
        event = json.loads(create.read_text(encoding="utf-8"))
        event["timestamp"] = _ns(f"2026-02-01T00:0{index}:00")
        create.write_text(json.dumps(event), encoding="utf-8")

    result = duplicate_title_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00")

    assert result == {"peak_hour": 0, "alarm_threshold": 10, "alarm": False}


def test_duplicate_title_peak_hour_exempts_recurring_bridge_alerts(tmp_path):
    from rebar.metrics.event_metrics import duplicate_title_peak_hour

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for index in range(12):
        d = tracker / f"alert-0000-0000-{index:04d}"
        d.mkdir(parents=True)
        _write_event(
            d,
            _ns(f"2026-02-01T00:{index:02d}:00"),
            "CREATE",
            {
                "ticket_type": "bug",
                "title": f"[binding{'-'}drift] bridge{'-'}fsck found stale binding",
            },
            author="rebar-bridge[bot]",
        )

    result = duplicate_title_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00")

    assert result == {"peak_hour": 0, "alarm_threshold": 10, "alarm": False}


def test_empty_create_ranges_are_measured_zeroes(tmp_path):
    from rebar.metrics.event_metrics import create_volume_peak_hour, duplicate_title_peak_hour

    (tmp_path / ".tickets-tracker").mkdir()

    assert duplicate_title_peak_hour(
        str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00"
    ) == {
        "peak_hour": 0,
        "alarm_threshold": 10,
        "alarm": False,
    }
    assert create_volume_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00") == {
        "peak_hour": 0,
        "alarm_threshold": 50,
        "alarm": False,
    }


def test_mass_creation_volume_peak_hour_alarms_and_clears(tmp_path):
    from rebar.metrics.event_metrics import create_volume_peak_hour

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for index in range(51):
        d = _ticket(tracker, f"eeee-0000-0000-{index:04d}", title=f"Work {index}")
        create = next(d.glob("*-CREATE.json"))
        event = json.loads(create.read_text(encoding="utf-8"))
        event["timestamp"] = _ns(f"2026-02-01T00:{index % 60:02d}:00")
        create.write_text(json.dumps(event), encoding="utf-8")
    quiet = _ticket(tracker, "eeee-0000-0000-9999", title="Later quiet work")
    quiet_create = next(quiet.glob("*-CREATE.json"))
    event = json.loads(quiet_create.read_text(encoding="utf-8"))
    event["timestamp"] = _ns("2026-02-01T03:00:00")
    quiet_create.write_text(json.dumps(event), encoding="utf-8")

    burst = create_volume_peak_hour(str(tmp_path), "2026-02-01T00:00:00", "2026-02-01T01:00:00")
    cleared = create_volume_peak_hour(str(tmp_path), "2026-02-01T03:00:00", "2026-02-01T04:00:00")

    assert burst == {"peak_hour": 51, "alarm_threshold": 50, "alarm": True}
    assert cleared == {"peak_hour": 1, "alarm_threshold": 50, "alarm": False}


# ── held-out oracles (merged from test_event_metrics_heldout) ──


def _write_event_heldout(
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


def _ticket_heldout(tracker: Path, tid: str, ttype: str = "task") -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event_heldout(
        d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": ttype, "title": tid}
    )
    return d


def _status(d: Path, ts_iso: str, cur: str, new: str, **extra) -> None:
    _write_event_heldout(d, _ns(ts_iso), "STATUS", {"status": new, "current_status": cur, **extra})


def test_rework_within_days_window(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # close -> reopen -> close, 3 days between reopen and re-close: within 7, not <2.
    d = _ticket_heldout(tracker, "cccc-0000-0000-0003")
    _status(d, "2026-02-01T00:00:00", "in_progress", "closed")
    _status(d, "2026-02-02T00:00:00", "closed", "open")
    _status(d, "2026-02-05T00:00:00", "in_progress", "closed")

    assert rework_within_days(str(tmp_path), 7) >= 1  # 3-day rework counts within 7
    assert rework_within_days(str(tmp_path), 2) == 0


def test_revert_recovery_substantive_only(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # Ticket A: a REVERT targeting a STATUS event -> counts.
    a = _ticket_heldout(tracker, "dddd-0000-0000-0004")
    _write_event_heldout(
        a,
        _ns("2026-02-03T00:00:00"),
        "REVERT",
        {"target_event_uuid": "x", "target_event_type": "STATUS", "reason": "r"},
    )
    # Ticket B: a REVERT targeting only a COMMENT -> does NOT count.
    b = _ticket_heldout(tracker, "eeee-0000-0000-0005")
    _write_event_heldout(
        b,
        _ns("2026-02-03T00:00:00"),
        "REVERT",
        {"target_event_uuid": "y", "target_event_type": "COMMENT", "reason": "r"},
    )

    n = revert_recovery(str(tmp_path))
    assert n == 1


def test_none_on_empty_range_but_zero_when_populated(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # An empty store: no qualifying tickets at all -> None (=> Unavailable).
    assert rework_within_days(str(tmp_path), 7, since="2026-01-01", until="2026-12-31") is None

    # A populated store with a ticket that has NO rework -> measured 0, not None.
    d = _ticket_heldout(tracker, "ffff-0000-0000-0006")
    _status(d, "2026-02-01T00:00:00", "in_progress", "closed")  # closed once, never reopened
    assert rework_within_days(str(tmp_path), 7, since="2026-01-01", until="2026-12-31") == 0


def test_retired_events_still_counted(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket_heldout(tracker, "0007-0000-0000-0007")
    # A retired (compaction-folded) claim event must still be counted.
    _write_event_heldout(
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
