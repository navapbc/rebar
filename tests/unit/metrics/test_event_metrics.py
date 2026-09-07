"""Contract tests for ``attempts_per_ticket`` in ``rebar.metrics.event_metrics``.

Raw events supply claim-session identity absent from compiled state. The reader counts distinct
sessions from ``open->in_progress`` status events within the requested range. Its result maps
each ticket ID to an attempt count.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.event_metrics import attempts_per_ticket

pytestmark = pytest.mark.unit

_ENV = "eeee-0000-4000-8000-000000000001"


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _write_event(ticket_dir: Path, ts_ns: int, event_type: str, data: dict) -> None:
    ev_uuid = str(uuid.uuid4())
    ev = {
        "event_type": event_type,
        "timestamp": ts_ns,
        "uuid": ev_uuid,
        "env_id": _ENV,
        "author": "t",
        "data": data,
    }
    (ticket_dir / f"{ts_ns:020d}-{ev_uuid}-{event_type}.json").write_text(
        json.dumps(ev), encoding="utf-8"
    )


def _ticket(tracker: Path, tid: str, ttype: str = "task") -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event(d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": ttype, "title": tid})
    return d


def _claim(ticket_dir: Path, ts_iso: str, session: str) -> None:
    _write_event(
        ticket_dir,
        _ns(ts_iso),
        "STATUS",
        {"status": "in_progress", "current_status": "open", "session": session},
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
