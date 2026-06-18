"""WS-C3: durable run-state + marker-after-effect idempotent resume (event store).

Drives the executor with a real target ticket so the TicketEventRecorder writes
WORKFLOW_RUN/WORKFLOW_STEP events, then proves: run-state reads back via replay;
a resumed run with the same run_id SKIPS already-marked steps (idempotency);
captured non-determinism is persisted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.workflow import executor as ex

pytest.importorskip("jsonschema")


def _wf(steps):
    return {"schema_version": "1", "name": "persist_demo", "steps": steps}


def test_run_state_persists_to_ticket(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    doc = _wf([{"id": "a", "uses": "echo", "with": {"v": "x"}}])
    res = ex.run_workflow(
        doc,
        run_id="RUN1",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"echo": lambda c: {"echoed": c.inputs["v"]}},
    )
    assert res.status == "succeeded"
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_runs"]["RUN1"]["status"] == "succeeded"
    step = state["workflow_steps"]["RUN1"]["a"]
    assert step["status"] == "succeeded"
    assert step["outputs"] == {"echoed": "x"}
    assert isinstance(step["captured"]["now_ns"], int)


def test_resume_skips_already_marked_step(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    calls = {"n": 0}

    def counting(ctx):
        calls["n"] += 1
        return {"n": calls["n"]}

    doc = _wf([{"id": "a", "uses": "count"}])
    # First run executes the step (writes the marker AFTER the effect).
    ex.run_workflow(
        doc,
        run_id="R",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"count": counting},
    )
    assert calls["n"] == 1
    # A second run with the SAME run_id finds the committed marker and SKIPS the
    # step — the effect is not repeated (idempotent resume).
    ex.run_workflow(
        doc,
        run_id="R",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"count": counting},
    )
    assert calls["n"] == 1, "step re-ran despite a committed marker"


def test_distinct_run_id_does_not_skip(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    calls = {"n": 0}

    def counting(ctx):
        calls["n"] += 1
        return {"n": calls["n"]}

    doc = _wf([{"id": "a", "uses": "count"}])
    ex.run_workflow(
        doc,
        run_id="RUN_A",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"count": counting},
    )
    ex.run_workflow(
        doc,
        run_id="RUN_B",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"count": counting},
    )
    assert calls["n"] == 2  # a different run is independent


def test_failed_step_is_not_marked_succeeded(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))

    def boom(ctx):
        raise RuntimeError("nope")

    doc = _wf([{"id": "a", "uses": "boom"}])
    res = ex.run_workflow(
        doc,
        run_id="RF",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"boom": boom},
    )
    assert res.status == "failed"
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["workflow_steps"]["RF"]["a"]["status"] == "failed"
