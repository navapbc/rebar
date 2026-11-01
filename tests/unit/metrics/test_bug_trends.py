"""Contract tests for the structural, high-confidence ``bug_trends`` lens.

Range-filtered flows report monthly close-class counts and time-to-close percentiles. Stock
metrics report open-bug age through the explicit ``now_ns`` clock seam, detection-channel
distribution, and ``caused_by`` fan-in ordered by descending count. Missing close classes use
``MISSING``. Empty populations return ``None`` so the registry renders unavailable.

Held-out oracles merged from test_bug_trends_heldout: HELD-OUT suite for the bug_trends metrics
lens (ticket b967).
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
    caused_by_provenance,
    close_class_by_month,
    detected_by_distribution,
    open_bug_age_days,
    time_to_close_days,
)

pytestmark = pytest.mark.unit


_ENV = "eeee-0000-4000-8000-000000000001"


_NS_PER_DAY = 86_400 * 1_000_000_000


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


def _bug(tracker: Path, tid: str, created_iso: str, **create_extra) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    data = {"ticket_type": "bug", "title": tid, **create_extra}
    _write_event(d, _ns(created_iso), "CREATE", data)
    return d


def _close(ticket_dir: Path, closed_iso: str, close_class: str | None = None) -> None:
    data: dict = {"status": "closed", "current_status": "open"}
    if close_class is not None:
        data["close_class"] = close_class
    _write_event(ticket_dir, _ns(closed_iso), "STATUS", data)


def test_close_class_by_month_labels_missing_cohort(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # Two classed closes in Feb, one classed in Mar, one PRE-CONVENTION close
    # (no close_class) in Feb — the MISSING cohort must be a labeled key.
    _close(
        _bug(tracker, "aaaa-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-02-10T00:00:00",
        "regression",
    )
    _close(
        _bug(tracker, "aaaa-0000-0000-0002", "2026-01-01T00:00:00"),
        "2026-02-15T00:00:00",
        "regression",
    )
    _close(
        _bug(tracker, "aaaa-0000-0000-0003", "2026-01-01T00:00:00"), "2026-03-05T00:00:00", "flaky"
    )
    _close(_bug(tracker, "aaaa-0000-0000-0004", "2026-01-01T00:00:00"), "2026-02-20T00:00:00", None)

    result = close_class_by_month(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert result == {
        "2026-02": {"regression": 2, "MISSING": 1},
        "2026-03": {"flaky": 1},
    }


def test_time_to_close_percentiles(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # 10 and 30 days to close -> nearest-rank p50=10, p90=30.
    _close(
        _bug(tracker, "bbbb-0000-0000-0001", "2026-01-01T00:00:00"),
        "2026-01-11T00:00:00",
        "regression",
    )
    _close(
        _bug(tracker, "bbbb-0000-0000-0002", "2026-01-01T00:00:00"), "2026-01-31T00:00:00", "flaky"
    )

    result = time_to_close_days(str(tmp_path), since="2026-01-01", until="2026-12-31")
    assert result == {"p50": 10.0, "p90": 30.0, "count": 2}


def test_open_bug_age_percentiles_with_clock_seam(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _bug(tracker, "cccc-0000-0000-0001", "2026-01-01T00:00:00")
    _bug(tracker, "cccc-0000-0000-0002", "2026-01-11T00:00:00")
    # A closed bug must NOT count toward open age.
    _close(
        _bug(tracker, "cccc-0000-0000-0003", "2026-01-01T00:00:00"), "2026-01-02T00:00:00", "flaky"
    )

    now = _ns("2026-01-21T00:00:00")
    result = open_bug_age_days(str(tmp_path), now_ns=now)
    assert result == {"p50": 10.0, "p90": 20.0, "max": 20.0, "count": 2}


def test_detected_by_distribution_with_unset_cohort(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _bug(tracker, "dddd-0000-0000-0001", "2026-01-01T00:00:00", detected_by="ci")
    _bug(tracker, "dddd-0000-0000-0002", "2026-01-02T00:00:00", detected_by="ci")
    _bug(tracker, "dddd-0000-0000-0003", "2026-01-03T00:00:00", detected_by="human_review")
    _bug(tracker, "dddd-0000-0000-0004", "2026-01-04T00:00:00")  # unset

    result = detected_by_distribution(str(tmp_path))
    assert result == {"ci": 2, "human_review": 1, "unset": 1}


def test_caused_by_fan_in_descending(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    target_hot = "ffff-0000-0000-00aa"
    target_cold = "ffff-0000-0000-00bb"
    for i, target in enumerate((target_hot, target_hot, target_cold)):
        d = _bug(tracker, f"eeee-0000-0000-000{i}", "2026-01-01T00:00:00")
        _write_event(
            d,
            _ns(f"2026-01-0{i + 2}T00:00:00"),
            "LINK",
            {"target_id": target, "relation": "caused_by"},
        )
    # A non-caused_by link must not count.
    d = _bug(tracker, "eeee-0000-0000-0009", "2026-01-01T00:00:00")
    _write_event(
        d, _ns("2026-01-09T00:00:00"), "LINK", {"target_id": target_cold, "relation": "relates_to"}
    )

    result = caused_by_fan_in(str(tmp_path))
    assert result == {target_hot: 2, target_cold: 1}
    assert list(result) == [target_hot, target_cold]


def test_caused_by_provenance_split(tmp_path):
    """Explicit / derived / unknown edge counts (ticket 6536-367c).

    An unmarked (pre-marker) edge counts as "unknown" — never merged into a real
    cohort — and non-caused_by links never count.
    """
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    target = "ffff-0000-0000-00aa"
    for i, provenance in enumerate(("explicit", "explicit", "derived", None)):
        d = _bug(tracker, f"abab-0000-0000-000{i}", "2026-01-01T00:00:00")
        data: dict = {"target_id": target, "relation": "caused_by"}
        if provenance is not None:
            data["provenance"] = provenance
        _write_event(d, _ns(f"2026-01-0{i + 2}T00:00:00"), "LINK", data)
    # A non-caused_by link must not count, marked or not.
    d = _bug(tracker, "abab-0000-0000-0009", "2026-01-01T00:00:00")
    _write_event(
        d, _ns("2026-01-09T00:00:00"), "LINK", {"target_id": target, "relation": "relates_to"}
    )

    result = caused_by_provenance(str(tmp_path))
    assert result == {"explicit": 2, "derived": 1, "unknown": 1}


def test_caused_by_provenance_none_when_no_edges(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _bug(tracker, "abab-0000-0000-0001", "2026-01-01T00:00:00")

    assert caused_by_provenance(str(tmp_path)) is None


def test_specs_registered_under_bug_trends_lens():
    import rebar.metrics  # hydrates REGISTRY via package-import side effects

    specs = {s.id: s for s in rebar.metrics.REGISTRY if s.lens == "bug_trends"}
    assert set(specs) == {
        "bug_close_class_by_month",
        "bug_time_to_close_days",
        "bug_open_age_days",
        "bug_detected_by_distribution",
        "bug_caused_by_fan_in",
        "bug_caused_by_provenance",
    }
    for spec in specs.values():
        assert spec.source == "structural"
        assert spec.confidence == "high"
    # Ids stay globally unique across the whole registry.
    all_ids = [s.id for s in rebar.metrics.REGISTRY]
    assert len(all_ids) == len(set(all_ids))


# ── held-out oracles (merged from test_bug_trends_heldout) ──


def _ticket(tracker: Path, tid: str, created_iso: str, ttype: str = "bug", **extra) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write_event(d, _ns(created_iso), "CREATE", {"ticket_type": ttype, "title": tid, **extra})
    return d


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
        check=False,
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
