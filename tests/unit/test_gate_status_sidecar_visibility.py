"""Plan-review gate_status findings-readability guards."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import rebar
from rebar import _mcp_inflight as inflight
from rebar.llm import gate_runs


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-01c4")
    rebar.init_repo(repo_root=str(repo))
    inflight.reset_registry()
    return repo


def _review_result_event(finding_id: str, *, schema: str = "plan_review_result_v2") -> str:
    return json.dumps(
        {
            "event_type": "REVIEW_RESULT",
            "data": {
                "schema": schema,
                "findings": [{"id": finding_id, "finding": finding_id}],
            },
        }
    )


def _write_review_result(
    tracker: Path,
    ticket_id: str,
    reviewed_at: int,
    finding_id: str,
    *,
    raw: str | None = None,
    schema: str = "plan_review_result_v2",
) -> None:
    ticket_dir = tracker / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{reviewed_at}-00000000-0000-4000-8000-000000000000-REVIEW_RESULT.json"
    body = raw if raw is not None else _review_result_event(finding_id, schema=schema)
    (ticket_dir / filename).write_text(body, encoding="utf-8")


def test_gate_status_marks_findings_unreadable_until_current_sidecar_exists(store: Path) -> None:
    tracker = store / ".tickets-tracker"
    tid = rebar.create_ticket("bug", "plan review sidecar race")
    job_started_at = time.time_ns()
    old_reviewed_at = job_started_at - 100
    current_reviewed_at = job_started_at + 100
    _write_review_result(tracker, tid, old_reviewed_at, "old-finding")
    gate_runs.record_gate_run(
        {
            "job_id": f"{job_started_at}-job",
            "ticket_id": tid,
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "BLOCK",
            "sidecar_emitted": True,
            "sidecar_reviewed_at": current_reviewed_at,
            "finished_at": time.time(),
        }
    )

    out = gate_runs.gate_run_status(f"{job_started_at}-job")
    assert out["status"] == "passed"
    assert out["verdict"] == "BLOCK"
    assert out["findings"]["sidecar_type"] == "REVIEW_RESULT"
    assert out["findings"]["readable"] is False
    assert out["findings"]["reason"] == "review-result-sidecar-missing"
    assert out["findings"]["reviewed_at"] == current_reviewed_at
    assert out["findings"]["latest_reviewed_at"] == old_reviewed_at

    _write_review_result(tracker, tid, current_reviewed_at, "current-finding")
    out = gate_runs.gate_run_status(f"{job_started_at}-job")
    assert out["findings"]["readable"] is True
    assert out["findings"]["reason"] == "current-review-result-sidecar"


def test_running_plan_review_status_does_not_scan_review_result_sidecars(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = rebar.create_ticket("bug", "running poll avoids sidecar scan")
    job_started_at = time.time_ns()

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("running polls should not scan REVIEW_RESULT sidecars")

    monkeypatch.setattr(gate_runs, "_latest_review_result_timestamp", _fail_if_called)
    gate_runs.record_gate_run(
        {
            "job_id": f"{job_started_at}-running",
            "ticket_id": tid,
            "gate_type": "plan_review",
            "status": "running",
            "sidecar_emitted": True,
            "started_at": time.time(),
            "finished_at": None,
        }
    )

    out = gate_runs.gate_run_status(f"{job_started_at}-running")
    assert out["findings"]["reason"] == "run-running"


def test_gate_status_distinguishes_unavailable_and_unparseable_findings(store: Path) -> None:
    tracker = store / ".tickets-tracker"
    tid = rebar.create_ticket("bug", "sidecar unavailable")
    job_started_at = time.time_ns()
    _write_review_result(tracker, tid, job_started_at + 1, "bad", raw="{not-json")
    cases = [
        ("failed-sidecar", {"sidecar_emitted": False}, "sidecar-not-emitted"),
        (
            "bad-sidecar",
            {"sidecar_emitted": True, "sidecar_reviewed_at": job_started_at + 1},
            "review-result-sidecar-missing",
        ),
    ]
    for suffix, fields, reason in cases:
        gate_runs.record_gate_run(
            {
                "job_id": f"{job_started_at}-{suffix}",
                "ticket_id": tid,
                "gate_type": "plan_review",
                "status": "passed",
                "verdict": "BLOCK",
                "finished_at": time.time(),
                **fields,
            }
        )
        out = gate_runs.gate_run_status(f"{job_started_at}-{suffix}")
        assert out["findings"]["readable"] is False
        assert out["findings"]["reason"] == reason


def test_gate_status_treats_reused_sidecar_receipt_as_readable(store: Path) -> None:
    tracker = store / ".tickets-tracker"
    tid = rebar.create_ticket("bug", "reused sidecar")
    reviewed_at = time.time_ns()
    _write_review_result(tracker, tid, reviewed_at, "current-finding")
    gate_runs.record_gate_run(
        {
            "job_id": f"{reviewed_at}-reuse",
            "ticket_id": tid,
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "BLOCK",
            "sidecar_emitted": False,
            "sidecar_reviewed_at": reviewed_at,
            "finished_at": time.time(),
        }
    )

    out = gate_runs.gate_run_status(f"{reviewed_at}-reuse")
    assert out["findings"]["readable"] is True
    assert out["findings"]["reason"] == "current-review-result-sidecar"


def test_plan_review_daemon_records_the_exact_returned_sidecar_receipt(store: Path) -> None:
    from rebar._mcp_llm import _spawn_gate_daemon

    tracker = store / ".tickets-tracker"
    tid = rebar.create_ticket("bug", "daemon sidecar receipt")
    reviewed_at = time.time_ns()
    newer_reviewed_at = reviewed_at + 100
    _write_review_result(tracker, tid, reviewed_at, "current-finding")
    _write_review_result(tracker, tid, newer_reviewed_at, "other-finding")
    inflight.reset_registry()
    handle = inflight.begin_gate_job("plan_review", tid, variant="source=attested")

    def result() -> dict:
        return {
            "verdict": "BLOCK",
            "ticket_id": tid,
            "sidecar_emitted": True,
            "sidecar_reviewed_at": reviewed_at,
        }

    _spawn_gate_daemon(handle, "plan_review", tid, result)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(handle.job_id)["status"] == "passed":
            break
        time.sleep(0.02)

    out = gate_runs.gate_run_status(handle.job_id)
    assert out["findings"]["readable"] is True
    assert out["findings"]["reviewed_at"] == reviewed_at
    assert out["findings"]["latest_reviewed_at"] == newer_reviewed_at


def test_poll_then_read_sequence_does_not_read_previous_plan_review_findings(store: Path) -> None:
    from rebar.llm.plan_review import sidecar

    tracker = store / ".tickets-tracker"
    tid = rebar.create_ticket("bug", "poll then read race")
    old_job_start = time.time_ns()
    old_reviewed_at = old_job_start + 10
    _write_review_result(tracker, tid, old_reviewed_at, "old-finding")
    for job_id, reviewed_at in (
        (f"{old_job_start}-old", old_reviewed_at),
        (f"{old_job_start + 1_000}-new", old_job_start + 1_010),
    ):
        gate_runs.record_gate_run(
            {
                "job_id": job_id,
                "ticket_id": tid,
                "gate_type": "plan_review",
                "status": "passed",
                "verdict": "BLOCK",
                "sidecar_emitted": True,
                "sidecar_reviewed_at": reviewed_at,
                "finished_at": time.time(),
            }
        )

    def read_current_findings(job_id: str) -> list[str]:
        status = gate_runs.gate_run_status(job_id)
        if status["status"] not in {"passed", "failed"} or not status["findings"]["readable"]:
            return []
        result = sidecar.latest_review_result(tid) or {}
        return [str(f.get("id")) for f in result.get("findings", [])]

    new_job_id = f"{old_job_start + 1_000}-new"
    new_reviewed_at = old_job_start + 1_010
    assert read_current_findings(f"{old_job_start}-old") == ["old-finding"]
    assert read_current_findings(new_job_id) == []
    _write_review_result(tracker, tid, new_reviewed_at, "new-finding")
    assert read_current_findings(new_job_id) == ["new-finding"]
