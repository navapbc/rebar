"""Happy-path contract for the gate-sidecar economics/env-diagnosis readers (ticket 3c07).

Tier: unit (real temp store; sidecar event files crafted directly). Pins the core
cost derivation that must read llm_calls from REVIEW_RESULT only (COMPLETION_VERDICT
has no metrics), and the env-diagnosis ERROR→PASS pairing. Gate-match / first-pass /
zero-accepted contracts are held out.

Public surface (from ``rebar.metrics.sidecar_metrics``):
- ``cost_per_accepted_change(repo_root, since=None, until=None) -> float | None``
- ``env_diagnosis_intervals(repo_root, since=None, until=None) -> list`` (each carries a duration)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rebar.metrics.sidecar_metrics import cost_per_accepted_change, env_diagnosis_intervals

pytestmark = pytest.mark.unit

_ENV = "eeee-0000-4000-8000-000000000001"


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _write(ticket_dir: Path, ts_ns: int, event_type: str, data: dict) -> None:
    u = str(uuid.uuid4())
    ev = {
        "event_type": event_type,
        "timestamp": ts_ns,
        "uuid": u,
        "env_id": _ENV,
        "author": "t",
        "data": data,
    }
    (ticket_dir / f"{ts_ns:020d}-{u}-{event_type}.json").write_text(
        json.dumps(ev), encoding="utf-8"
    )


def _ticket(tracker: Path, tid: str) -> Path:
    d = tracker / tid
    d.mkdir(parents=True)
    _write(d, _ns("2026-01-01T00:00:00"), "CREATE", {"ticket_type": "task", "title": tid})
    return d


def _review(d: Path, ts: str, verdict: str, llm_calls: int) -> None:
    _write(
        d,
        _ns(ts),
        "REVIEW_RESULT",
        {
            "schema": "plan_review_result_v2",
            "verdict": verdict,
            "gate": "plan_review",
            "metrics": {"llm_calls": llm_calls, "total_ms": 100},
        },
    )


def _completion_pass(d: Path, ts: str) -> None:
    _write(
        d,
        _ns(ts),
        "COMPLETION_VERDICT",
        {"schema": "completion_verifier_pass_v1", "verdict": "PASS"},
    )


def test_cost_per_accepted_change_from_review_metrics(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "aaaa-0000-0000-0001")
    # 3 plan-review runs: llm_calls 2 + 3 + 4 = 9; one accepted (completion PASS) close.
    _review(d, "2026-02-01T00:00:00", "BLOCK", 2)
    _review(d, "2026-02-02T00:00:00", "BLOCK", 3)
    _review(d, "2026-02-03T00:00:00", "PASS", 4)
    _completion_pass(d, "2026-02-04T00:00:00")

    assert cost_per_accepted_change(str(tmp_path)) == 9.0  # 9 llm_calls / 1 accepted


def test_env_diagnosis_interval_pairs_error_to_next_pass(tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    d = _ticket(tracker, "bbbb-0000-0000-0002")
    # A gate ERROR at T0, then a plan_review PASS 2 hours later -> interval == 2h in ns.
    _write(
        d,
        _ns("2026-02-01T00:00:00"),
        "REVIEW_RESULT",
        {
            "schema": "gate_error_v1",
            "verdict": "ERROR",
            "gate": "plan_review",
            "error": {"cause": "outage"},
        },
    )
    _review(d, "2026-02-01T02:00:00", "PASS", 1)

    intervals = env_diagnosis_intervals(str(tmp_path))
    assert intervals, "an ERROR followed by a same-gate PASS must yield one diagnosis interval"
    dur = intervals[0]["duration_ns"] if isinstance(intervals[0], dict) else intervals[0]
    assert dur == _ns("2026-02-01T02:00:00") - _ns("2026-02-01T00:00:00")
