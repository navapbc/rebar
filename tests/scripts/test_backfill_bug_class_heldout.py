"""Held-out contracts for the bug-close_class backfill (ticket 5062). WITHHELD.

- a plan-review BLOCK -> plan_defect; a gate_error_v1 ERROR -> env_integration,
- a bug already carrying close_class is SKIPPED (authoritative-value protection),
- every emitted record carries source=backfill_classified + confidence=classified and
  is NOT authoritative,
- a REVERT whose only target is a COMMENT does NOT yield regression (substantive filter).
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.registry import is_authoritative

pytestmark = pytest.mark.scripts

_ENV = "eeee-0000-4000-8000-000000000001"
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_bug_class.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_bug_class", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _write(d: Path, ts_ns: int, et: str, data: dict) -> None:
    u = str(uuid.uuid4())
    ev = {
        "event_type": et,
        "timestamp": ts_ns,
        "uuid": u,
        "env_id": _ENV,
        "author": "t",
        "data": data,
    }
    (d / f"{ts_ns:020d}-{u}-{et}.json").write_text(json.dumps(ev), encoding="utf-8")


def _closed_bug(tracker: Path, tid: str, close_class: str | None = None) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write(d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": "bug", "title": tid})
    _write(
        d, _ns("2026-01-02T00:00:00"), "STATUS", {"status": "in_progress", "current_status": "open"}
    )
    close_data = {"status": "closed", "current_status": "in_progress"}
    if close_class is not None:
        close_data["close_class"] = close_class
    _write(d, _ns("2026-01-05T00:00:00"), "STATUS", close_data)
    return d


def _by_ticket(mod, repo_root: str) -> dict:
    return {r["ticket_id"]: r for r in mod.classify_backfill(repo_root)}


def test_block_backfills_plan_defect(tmp_path):
    mod = _load()
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _closed_bug(tracker, "cccc-0000-0000-0003")
    _write(
        d,
        _ns("2026-01-03T00:00:00"),
        "REVIEW_RESULT",
        {"schema": "plan_review_result_v2", "verdict": "BLOCK"},
    )
    assert _by_ticket(mod, str(tmp_path))["cccc-0000-0000-0003"]["close_class"] == "plan_defect"


def test_gate_error_backfills_env_integration(tmp_path):
    mod = _load()
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _closed_bug(tracker, "dddd-0000-0000-0004")
    _write(
        d,
        _ns("2026-01-03T00:00:00"),
        "REVIEW_RESULT",
        {
            "schema": "gate_error_v1",
            "verdict": "ERROR",
            "gate": "plan_review",
            "error": {"cause": "x"},
        },
    )
    assert _by_ticket(mod, str(tmp_path))["dddd-0000-0000-0004"]["close_class"] == "env_integration"


def test_bug_with_existing_close_class_is_skipped(tmp_path):
    mod = _load()
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _closed_bug(tracker, "eeee-0000-0000-0005", close_class="flaky")  # authoritative already
    assert "eeee-0000-0000-0005" not in _by_ticket(mod, str(tmp_path))


def test_backfill_records_are_labeled_and_nonauthoritative(tmp_path):
    mod = _load()
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _closed_bug(tracker, "ffff-0000-0000-0006")
    rec = _by_ticket(mod, str(tmp_path))["ffff-0000-0000-0006"]
    assert rec["source"] == "backfill_classified"
    assert rec["confidence"] == "classified"
    assert is_authoritative(rec["source"]) is False


def test_comment_only_revert_not_regression(tmp_path):
    mod = _load()
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _closed_bug(tracker, "0007-0000-0000-0007")
    _write(
        d,
        _ns("2026-01-04T00:00:00"),
        "REVERT",
        {"target_event_uuid": "y", "target_event_type": "COMMENT", "reason": "r"},
    )
    # A revert of only a COMMENT is not substantive -> falls through to undetermined.
    assert _by_ticket(mod, str(tmp_path))["0007-0000-0000-0007"]["close_class"] == "undetermined"
