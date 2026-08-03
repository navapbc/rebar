"""Happy-path contract for the bug_trends metrics lens (ticket b967).

Tier: unit (real temp store; raw event files crafted with known timestamps).

Public surface (from ``rebar.metrics.bug_trends``) — five dimensions over the
bug population, each a multi-arg derivation plus a registered spec
(lens ``bug_trends``, source ``structural``, confidence ``high``):

- ``close_class_by_month(repo_root, since=None, until=None) -> dict | None`` —
  FLOW: ``{"YYYY-MM": {<close_class or "MISSING">: count}}`` over bugs whose
  close falls in range. The pre-convention MISSING cohort is a labeled key,
  never dropped or merged into a real class.
- ``time_to_close_days(repo_root, since=None, until=None) -> dict | None`` —
  FLOW: ``{"p50": days, "p90": days, "count": n}`` (nearest-rank percentiles)
  over bugs closed in range.
- ``open_bug_age_days(repo_root, now_ns=None) -> dict | None`` — STOCK:
  ``{"p50": days, "p90": days, "max": days, "count": n}`` over open +
  in_progress bugs; ``now_ns`` is an explicit clock seam.
- ``detected_by_distribution(repo_root) -> dict | None`` — STOCK:
  ``{<channel>: count, "unset": n}``; ``None`` when NO bug carries the field
  (the sibling-landing degradation path).
- ``caused_by_fan_in(repo_root) -> dict | None`` — STOCK: ``{target_id: count}``
  ordered by descending count over bugs' ``caused_by`` links.

``None`` means no data accrued (the registry renders ``unavailable``) — never
an empty-implies-healthy zero.
"""

from __future__ import annotations

import json
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
    assert list(result) == [target_hot, target_cold]  # descending fan-in order


def test_specs_registered_under_bug_trends_lens():
    import rebar.metrics  # hydrates REGISTRY via package-import side effects

    specs = {s.id: s for s in rebar.metrics.REGISTRY if s.lens == "bug_trends"}
    assert set(specs) == {
        "bug_close_class_by_month",
        "bug_time_to_close_days",
        "bug_open_age_days",
        "bug_detected_by_distribution",
        "bug_caused_by_fan_in",
    }
    for spec in specs.values():
        assert spec.source == "structural"
        assert spec.confidence == "high"
    # Ids stay globally unique across the whole registry.
    all_ids = [s.id for s in rebar.metrics.REGISTRY]
    assert len(all_ids) == len(set(all_ids))
