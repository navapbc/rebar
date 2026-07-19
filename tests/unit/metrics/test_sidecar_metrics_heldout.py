"""Held-out contracts for the gate-sidecar readers (ticket 3c07). WITHHELD.

- cost is read from REVIEW_RESULT metrics ONLY (COMPLETION_VERDICT has none, yet cost
  still computes),
- zero accepted closes -> None (no ZeroDivisionError),
- first_pass_verification uses the OLDEST completion verdict per ticket,
- env_diagnosis pairing matches on the ERROR's `gate` field (a plan_review ERROR is
  NOT closed by an interleaved completion PASS),
- the metrics register into the c085 registry with source="sidecar" (authoritative).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.registry import REGISTRY, is_authoritative
from rebar.metrics.sidecar_metrics import (
    cost_per_accepted_change,
    env_diagnosis_intervals,
    first_pass_verification,
)

pytestmark = pytest.mark.unit

_ENV = "eeee-0000-4000-8000-000000000001"


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


def _ticket(tracker: Path, tid: str) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write(d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": "task", "title": tid})
    return d


def _review(d: Path, ts: str, verdict: str, llm_calls: int, gate: str = "plan_review") -> None:
    _write(
        d,
        _ns(ts),
        "REVIEW_RESULT",
        {
            "schema": "plan_review_result_v2",
            "verdict": verdict,
            "gate": gate,
            "metrics": {"llm_calls": llm_calls},
        },
    )


def _completion(d: Path, ts: str, passed: bool) -> None:
    schema = "completion_verifier_pass_v1" if passed else "completion_verifier_fail_v1"
    _write(
        d,
        _ns(ts),
        "COMPLETION_VERDICT",
        {"schema": schema, "verdict": "PASS" if passed else "FAIL"},
    )


def test_cost_reads_review_metrics_only(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "cccc-0000-0000-0003")
    _review(d, "2026-02-01T00:00:00", "PASS", 5)
    # COMPLETION_VERDICT carries NO metrics block — cost still computes from review.
    _completion(d, "2026-02-02T00:00:00", passed=True)
    assert cost_per_accepted_change(str(tmp_path)) == 5.0


def test_zero_accepted_returns_none(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "dddd-0000-0000-0004")
    _review(d, "2026-02-01T00:00:00", "BLOCK", 3)
    _completion(d, "2026-02-02T00:00:00", passed=False)  # no accepted close
    assert cost_per_accepted_change(str(tmp_path)) is None  # no ZeroDivisionError


def test_first_pass_uses_oldest_completion(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    # 2 of 3 tickets pass on their FIRST (oldest) completion verdict.
    a = _ticket(tracker, "0a01-0000-0000-0001")
    _completion(a, "2026-02-01T00:00:00", passed=True)
    b = _ticket(tracker, "0b02-0000-0000-0002")
    _completion(b, "2026-02-01T00:00:00", passed=True)
    _completion(b, "2026-02-02T00:00:00", passed=False)  # later fail must not change first-pass
    c = _ticket(tracker, "0c03-0000-0000-0003")
    _completion(c, "2026-02-01T00:00:00", passed=False)  # first attempt failed
    _completion(c, "2026-02-02T00:00:00", passed=True)

    assert first_pass_verification(str(tmp_path)) == pytest.approx(2 / 3)


def test_env_diagnosis_matches_gate(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "eeee-0000-0000-0005")
    # plan_review ERROR, an interleaved COMPLETION PASS (other gate), then plan_review PASS.
    _write(
        d,
        _ns("2026-02-01T00:00:00"),
        "REVIEW_RESULT",
        {
            "schema": "gate_error_v1",
            "verdict": "ERROR",
            "gate": "plan_review",
            "error": {"cause": "x"},
        },
    )
    _completion(
        d, "2026-02-01T01:00:00", passed=True
    )  # completion PASS — must NOT close a plan_review ERROR
    _review(d, "2026-02-01T03:00:00", "PASS", 1)  # the real closing plan_review PASS

    intervals = env_diagnosis_intervals(str(tmp_path))
    assert len(intervals) == 1
    dur = intervals[0]["duration_ns"] if isinstance(intervals[0], dict) else intervals[0]
    # Must pair with the 03:00 plan_review PASS (3h), NOT the 01:00 completion PASS (1h).
    assert dur == _ns("2026-02-01T03:00:00") - _ns("2026-02-01T00:00:00")


def test_sidecar_metrics_registered_authoritative():
    specs = [s for s in REGISTRY if s.source == "sidecar"]
    assert specs, "3c07 must register at least one sidecar-sourced metric"
    for s in specs:
        assert is_authoritative(s.source) is True
