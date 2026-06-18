"""WS-C1: WORKFLOW_RUN / WORKFLOW_STEP run-state on the event store.

Run-state persists as append-only events on the TARGET ticket and folds into
ticket state as the lazy per-key maps ``workflow_runs`` / ``workflow_steps``. These
tests pin: the events reduce into state; per-key last-writer-wins; HLC+UUID
convergence (highest filename order wins); two runs on one ticket don't clobber;
a ticket that never ran a workflow keeps its exact shape (additive); and the state
survives compaction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rebar
from rebar._commands import _seam


def _tracker(repo: Path) -> Path:
    return _seam.tracker_dir(str(repo))


def _append(repo: Path, tid: str, etype: str, data: dict) -> None:
    _seam.append_event(tid, etype, data, _tracker(repo), repo_root=str(repo))


def _write_event_file(repo: Path, tid: str, etype: str, ts: int, uid: str, data: dict) -> Path:
    tracker = _tracker(repo)
    env_id = (tracker / ".env-id").read_text().strip()
    event = {
        "timestamp": ts,
        "uuid": uid,
        "event_type": etype,
        "env_id": env_id,
        "author": "test",
        "data": data,
    }
    path = tracker / tid / f"{ts}-{uid}-{etype}.json"
    path.write_text(json.dumps(event, ensure_ascii=False))
    return path


def test_run_and_step_recorded(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_RUN",
        {
            "run_id": "run-1",
            "workflow_name": "code_review",
            "status": "running",
        },
    )
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        {
            "run_id": "run-1",
            "step_id": "fetch",
            "status": "succeeded",
            "outputs": {"diff": "..."},
        },
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_runs"]["run-1"]["status"] == "running"
    assert state["workflow_runs"]["run-1"]["workflow_name"] == "code_review"
    assert state["workflow_steps"]["run-1"]["fetch"]["status"] == "succeeded"
    assert state["workflow_steps"]["run-1"]["fetch"]["outputs"] == {"diff": "..."}


def test_step_last_writer_wins(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        {
            "run_id": "r",
            "step_id": "s",
            "status": "running",
        },
    )
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        {
            "run_id": "r",
            "step_id": "s",
            "status": "succeeded",
            "outputs": {"x": 1},
        },
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    step = state["workflow_steps"]["r"]["s"]
    assert step["status"] == "succeeded"
    assert step["outputs"] == {"x": 1}


def test_run_status_transition_lww(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(rebar_repo, tid, "WORKFLOW_RUN", {"run_id": "r", "status": "running"})
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_RUN",
        {"run_id": "r", "status": "succeeded", "ended_at": "2026-06-18T00:00:00Z"},
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_runs"]["r"]["status"] == "succeeded"
    assert state["workflow_runs"]["r"]["ended_at"] == "2026-06-18T00:00:00Z"


def test_two_runs_do_not_clobber(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(rebar_repo, tid, "WORKFLOW_RUN", {"run_id": "a", "status": "succeeded"})
    _append(rebar_repo, tid, "WORKFLOW_RUN", {"run_id": "b", "status": "failed"})
    _append(rebar_repo, tid, "WORKFLOW_STEP", {"run_id": "a", "step_id": "s1", "status": "ok"})
    _append(rebar_repo, tid, "WORKFLOW_STEP", {"run_id": "b", "step_id": "s1", "status": "bad"})
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_runs"]["a"]["status"] == "succeeded"
    assert state["workflow_runs"]["b"]["status"] == "failed"
    assert state["workflow_steps"]["a"]["s1"]["status"] == "ok"
    assert state["workflow_steps"]["b"]["s1"]["status"] == "bad"


def test_convergence_highest_hlc_uuid_wins(rebar_repo: Path) -> None:
    # Write two competing step records as raw files with controlled prefixes; the
    # one that sorts last by {timestamp}-{uuid} (HLC+UUID order) must win on every
    # clone, regardless of which was written first.
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    base = 1_781_000_000_000_000_000
    # Write the LATER timestamp's file first, then the earlier — order on disk
    # doesn't matter; replay sorts by name.
    _write_event_file(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        base + 5,
        "ffffffff-0000-4000-8000-000000000002",
        {"run_id": "r", "step_id": "s", "status": "WINNER"},
    )
    _write_event_file(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        base + 1,
        "ffffffff-0000-4000-8000-000000000001",
        {"run_id": "r", "step_id": "s", "status": "loser"},
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_steps"]["r"]["s"]["status"] == "WINNER"


def test_non_workflow_ticket_has_no_workflow_keys(rebar_repo: Path) -> None:
    # A ticket that never ran a workflow must keep its exact prior shape — the maps
    # are lazy/additive, so no empty workflow_runs/workflow_steps key appears.
    tid = rebar.create_ticket("task", "Plain", repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "workflow_runs" not in state
    assert "workflow_steps" not in state


def test_survives_compaction(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(rebar_repo, tid, "WORKFLOW_RUN", {"run_id": "r", "status": "succeeded"})
    _append(
        rebar_repo,
        tid,
        "WORKFLOW_STEP",
        {"run_id": "r", "step_id": "s", "status": "ok", "outputs": {"k": "v"}},
    )
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "compact", tid, "--threshold=0"],
        capture_output=True,
        text=True,
        cwd=str(rebar_repo),
        env={**os.environ, "REBAR_SYNC_PULL": "off"},
    )
    assert cp.returncode == 0, cp.stderr
    snaps = list((_tracker(rebar_repo) / tid).glob("*-SNAPSHOT.json"))
    assert snaps, "expected a SNAPSHOT after compaction"
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_runs"]["r"]["status"] == "succeeded"
    assert state["workflow_steps"]["r"]["s"]["outputs"] == {"k": "v"}


def test_malformed_workflow_events_ignored(rebar_repo: Path) -> None:
    # Missing run_id / step_id => the event is a no-op (no crash, no partial map).
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append(rebar_repo, tid, "WORKFLOW_RUN", {"status": "running"})  # no run_id
    _append(rebar_repo, tid, "WORKFLOW_STEP", {"run_id": "r", "status": "x"})  # no step_id
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "workflow_runs" not in state
    assert "workflow_steps" not in state
