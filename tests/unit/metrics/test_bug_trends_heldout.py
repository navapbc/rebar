"""HELD-OUT suite for the bug_trends metrics lens (ticket b967).

Not shown to the implementer. Pins the edge/degradation contract: zero-bug
unavailability, dimension independence, era-skew labeling, stock-vs-flow range
semantics, percentile edges, unresolved targets, non-bug exclusion, and the
end-to-end CLI fault-isolation rendering.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.bug_trends import (
    caused_by_fan_in,
    close_class_by_month,
    detected_by_distribution,
    open_bug_age_days,
    time_to_close_days,
)

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


def _ticket(tracker: Path, tid: str, created_iso: str, ttype: str = "bug", **extra) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event(d, _ns(created_iso), "CREATE", {"ticket_type": ttype, "title": tid, **extra})
    return d


def _close(ticket_dir: Path, closed_iso: str, close_class: str | None = None) -> None:
    data: dict = {"status": "closed", "current_status": "open"}
    if close_class is not None:
        data["close_class"] = close_class
    _write_event(ticket_dir, _ns(closed_iso), "STATUS", data)


# ── zero-bug / no-tracker unavailability (never empty-implies-healthy) ────────


def test_all_dimensions_none_on_empty_tracker(tmp_path):
    (tmp_path / ".tickets-tracker").mkdir()
    assert close_class_by_month(str(tmp_path)) is None
    assert time_to_close_days(str(tmp_path)) is None
    assert open_bug_age_days(str(tmp_path)) is None
    assert detected_by_distribution(str(tmp_path)) is None
    assert caused_by_fan_in(str(tmp_path)) is None


def test_all_dimensions_none_when_tracker_missing(tmp_path):
    assert close_class_by_month(str(tmp_path)) is None
    assert time_to_close_days(str(tmp_path)) is None
    assert open_bug_age_days(str(tmp_path)) is None
    assert detected_by_distribution(str(tmp_path)) is None
    assert caused_by_fan_in(str(tmp_path)) is None


def test_non_bug_tickets_are_excluded(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # Closed tasks/stories never contribute to any bug dimension.
    _close(
        _ticket(tracker, "aaaa-0000-0000-0001", "2026-01-01T00:00:00", ttype="task"),
        "2026-01-05T00:00:00",
    )
    _ticket(tracker, "aaaa-0000-0000-0002", "2026-01-01T00:00:00", ttype="story", detected_by="ci")
    assert close_class_by_month(str(tmp_path), since="2026-01-01", until="2026-12-31") is None
    assert time_to_close_days(str(tmp_path), since="2026-01-01", until="2026-12-31") is None
    assert open_bug_age_days(str(tmp_path)) is None
    assert detected_by_distribution(str(tmp_path)) is None


# ── dimension independence (sparse detected_by degrades alone) ────────────────


def test_absent_detected_by_degrades_only_that_dimension(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _close(
        _ticket(tracker, "bbbb-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-01-11T00:00:00",
        "regression",
    )
    assert detected_by_distribution(str(tmp_path)) is None
    months = close_class_by_month(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert months == {"2026-01": {"regression": 1}}
    ttc = time_to_close_days(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert ttc is not None and ttc["count"] == 1


# ── era-skew labeling ─────────────────────────────────────────────────────────


def test_all_missing_month_is_labeled_not_dropped(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    for i in range(3):
        _close(
            _ticket(tracker, f"cccc-0000-0000-000{i}", "2026-01-01T00:00:00"),
            "2026-02-10T00:00:00",
            None,
        )
    months = close_class_by_month(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert months == {"2026-02": {"MISSING": 3}}


def test_missing_never_merged_into_a_real_class(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _close(
        _ticket(tracker, "dddd-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-02-10T00:00:00",
        "flaky",
    )
    _close(
        _ticket(tracker, "dddd-0000-0000-0002", "2026-01-01T00:00:00"), "2026-02-11T00:00:00", None
    )
    months = close_class_by_month(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert months["2026-02"] == {"flaky": 1, "MISSING": 1}


# ── stock vs flow range semantics ─────────────────────────────────────────────


def test_flow_dimensions_filter_on_close_time(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _close(
        _ticket(tracker, "eeee-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-02-10T00:00:00",
        "regression",
    )
    _close(
        _ticket(tracker, "eeee-0000-0000-0002", "2026-01-01T00:00:00"),
        "2026-06-10T00:00:00",
        "flaky",
    )
    months = close_class_by_month(str(tmp_path), since="2026-02-01", until="2026-02-28")
    assert months == {"2026-02": {"regression": 1}}
    ttc = time_to_close_days(str(tmp_path), since="2026-02-01", until="2026-02-28")
    assert ttc is not None and ttc["count"] == 1


def test_stock_dimensions_ignore_range_and_see_old_bugs(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _ticket(tracker, "ffff-0000-0000-0001", "2020-01-01T00:00:00", detected_by="ci")
    now = _ns("2026-01-01T00:00:00")
    ages = open_bug_age_days(str(tmp_path), now_ns=now)
    assert ages is not None and ages["count"] == 1
    assert ages["max"] > 2000  # ~6 years old, well outside any 30-day window
    assert detected_by_distribution(str(tmp_path)) == {"ci": 1, "unset": 0}


# ── percentile edges ──────────────────────────────────────────────────────────


def test_single_sample_percentiles_collapse(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _close(
        _ticket(tracker, "abab-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-01-08T00:00:00",
        "flaky",
    )
    ttc = time_to_close_days(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert ttc == {"p50": 7.0, "p90": 7.0, "count": 1}


def test_in_progress_bugs_count_as_open_for_age(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "acac-0000-0000-0001", "2026-01-01T00:00:00")
    _write_event(
        d, _ns("2026-01-02T00:00:00"), "STATUS", {"status": "in_progress", "current_status": "open"}
    )
    ages = open_bug_age_days(str(tmp_path), now_ns=_ns("2026-01-11T00:00:00"))
    assert ages is not None and ages["count"] == 1 and ages["max"] == 10.0


# ── caused_by fan-in edges ────────────────────────────────────────────────────


def test_unresolved_caused_by_target_still_counted(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "adad-0000-0000-0001", "2026-01-01T00:00:00")
    # Target ticket dir does not exist; the verbatim id must still be counted.
    _write_event(
        d,
        _ns("2026-01-02T00:00:00"),
        "LINK",
        {"target_id": "dead-beef-0000-0000", "relation": "caused_by"},
    )
    assert caused_by_fan_in(str(tmp_path)) == {"dead-beef-0000-0000": 1}


def test_unlinked_caused_by_not_counted(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "aeae-0000-0000-0001", "2026-01-01T00:00:00")
    link_ts = _ns("2026-01-02T00:00:00")
    link_uuid = str(uuid.uuid4())
    ev = {
        "event_type": "LINK",
        "timestamp": link_ts,
        "uuid": link_uuid,
        "env_id": _ENV,
        "author": "t",
        "data": {"target_id": "dead-beef-0000-0001", "relation": "caused_by"},
    }
    (d / f"{link_ts:020d}-{link_uuid}-LINK.json").write_text(json.dumps(ev), encoding="utf-8")
    _write_event(d, _ns("2026-01-03T00:00:00"), "UNLINK", {"link_uuid": link_uuid})
    assert caused_by_fan_in(str(tmp_path)) is None


# ── registry + CLI integration (fault isolation, labels) ─────────────────────


def test_registry_evaluate_returns_unavailable_with_reason(tmp_path):
    from types import SimpleNamespace

    import rebar.metrics

    (tmp_path / ".tickets-tracker").mkdir()
    ctx = SimpleNamespace(repo_root=str(tmp_path), since="2026-01-01", until="2026-12-31")
    for spec in rebar.metrics.REGISTRY:
        if spec.lens != "bug_trends":
            continue
        result = rebar.metrics.evaluate(spec, ctx)
        assert isinstance(result, rebar.metrics.Unavailable)
        assert result.reason and result.accruing_since


def test_registry_evaluate_returns_labeled_value(tmp_path):
    from types import SimpleNamespace

    import rebar.metrics

    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _close(
        _ticket(tracker, "afaf-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-01-11T00:00:00",
        "regression",
    )
    ctx = SimpleNamespace(repo_root=str(tmp_path), since="2026-01-01", until="2026-12-31")
    spec = next(s for s in rebar.metrics.REGISTRY if s.id == "bug_close_class_by_month")
    result = rebar.metrics.evaluate(spec, ctx)
    assert isinstance(result, rebar.metrics.MetricValue)
    assert result.source == "structural" and result.confidence == "high"
    assert result.value == {"2026-01": {"regression": 1}}


def test_metrics_cli_renders_all_bug_trend_ids(tmp_path):
    """End-to-end: `rebar metrics` always shows every bug_trends id (value or
    unavailable), even on a store with no bugs — fault isolation contract."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    cp = subprocess.run(
        [sys.executable, "-m", "rebar", "metrics", "--output", "json"],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "REBAR_ROOT": str(tmp_path),
            "REBAR_SYNC_PULL": "off",
            "REBAR_SYNC_PUSH": "off",
        },
        cwd=str(tmp_path),
    )
    assert cp.returncode == 0, cp.stderr
    doc = json.loads(cp.stdout)
    for mid in (
        "bug_close_class_by_month",
        "bug_time_to_close_days",
        "bug_open_age_days",
        "bug_detected_by_distribution",
        "bug_caused_by_fan_in",
    ):
        assert mid in doc["metrics"], f"{mid} missing from metrics output"
        entry = doc["metrics"][mid]
        assert ("unavailable" in entry) or ("value" in entry)
