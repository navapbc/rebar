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


# ── v2 control flow through the REAL recorder + reducer (dbc6) ──────────────────


def _loop_wf():
    # A bounded loop: the body runs until the recorded `done` flag (i>=2), so it
    # executes iterations 0,1,2 — each persisted under its own iteration frame key.
    return {
        "schema_version": "2",
        "name": "loop_demo",
        "steps": [
            {"id": "start", "uses": "echo", "with": {"v": "go"}},
            {
                "id": "L",
                "needs": ["start"],
                "loop": {
                    "max_iterations": 5,
                    "until": "${{ steps.attempt.outputs.done }}",
                    "var": "i",
                    "body": [{"id": "attempt", "uses": "score"}],
                },
            },
        ],
    }


def _v2_registry(calls):
    def score(ctx):
        calls.append(ctx.frame_key)
        return {"done": (ctx.iteration or 0) >= 2, "score": (ctx.iteration or 0) + 1}

    return {"echo": lambda c: {"echoed": c.inputs["v"]}, "score": score}


def test_v2_loop_persists_iteration_keyed_markers(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    res = ex.run_workflow(
        _loop_wf(),
        run_id="V2",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry=_v2_registry([]),
    )
    assert res.status == "succeeded"
    steps = rebar.show_ticket(tid, repo_root=str(rebar_repo))["workflow_steps"]["V2"]
    # Each loop iteration is a DISTINCT marker keyed by its frame path; the flat top
    # frame keeps bare ids (start, L) — no nested per-iteration dict (hot path stays flat).
    assert {"start", "L", "L#0/attempt", "L#1/attempt", "L#2/attempt"} <= set(steps)
    assert "L#3/attempt" not in steps
    assert steps["L"]["outputs"] == {"iterations": 3}
    assert steps["L#2/attempt"]["iteration"] == 2


def test_v2_loop_resume_reruns_nothing(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    calls: list[str] = []
    reg = _v2_registry(calls)
    ex.run_workflow(
        _loop_wf(),
        run_id="V2R",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry=reg,
    )
    assert calls == ["L#0/attempt", "L#1/attempt", "L#2/attempt"]
    # A resume with the SAME run_id finds every iteration's committed marker and
    # re-runs no body step — exactly-once across the (simulated) restart.
    ex.run_workflow(
        _loop_wf(),
        run_id="V2R",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry=reg,
    )
    assert calls == ["L#0/attempt", "L#1/attempt", "L#2/attempt"], (
        "a loop iteration re-ran on resume"
    )


def test_v2_map_persists_iteration_keyed_markers(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    wf = {
        "schema_version": "2",
        "name": "mapdemo",
        "inputs": {"xs": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.xs }}",
                    "as": "x",
                    "body": [{"id": "proc", "uses": "echo", "with": {"v": "${{ map.x }}"}}],
                },
            }
        ],
    }
    res = ex.run_workflow(
        wf,
        {"xs": ["a", "b"]},
        run_id="MAP",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"echo": lambda c: {"echoed": c.inputs["v"]}},
    )
    assert res.status == "succeeded"
    steps = rebar.show_ticket(tid, repo_root=str(rebar_repo))["workflow_steps"]["MAP"]
    # The reducer must key the @/# frame paths correctly through show_ticket reconstruction.
    assert {"M", "M#0/proc", "M#1/proc"} <= set(steps)
    assert steps["M#0/proc"]["outputs"] == {"echoed": "a"}
    assert steps["M"]["outputs"] == {"count": 2}


def test_v2_branch_persists_only_the_chosen_arm(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    wf = {
        "schema_version": "2",
        "name": "brdemo",
        "inputs": {"flag": {"type": "boolean"}},
        "steps": [
            {
                "id": "g",
                "branch": {
                    "when": "${{ inputs.flag }}",
                    "then": [{"id": "yes", "uses": "noop"}],
                    "else": [{"id": "no", "uses": "noop"}],
                },
            }
        ],
    }
    ex.run_workflow(
        wf,
        {"flag": True},
        run_id="BR",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"noop": lambda c: {}},
    )
    steps = rebar.show_ticket(tid, repo_root=str(rebar_repo))["workflow_steps"]["BR"]
    assert "g@then/yes" in steps and steps["g"]["outputs"] == {"taken": "then"}
    assert "g@else/no" not in steps  # the unchosen arm never ran


def test_v2_loop_partial_resume_through_real_store(rebar_repo: Path) -> None:
    # A run that FAILS partway (iteration 2 errors), then a fresh resume with the same
    # run_id: the committed iterations 0,1 are idempotent-skipped and only iteration 2
    # re-runs — exactly-once across the (simulated) restart, through the real reducer.
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    calls: list[str] = []

    def flaky(ctx):
        calls.append(ctx.frame_key)
        if ctx.iteration == 2 and "fixed" not in calls:
            raise RuntimeError("transient on iter 2")
        return {"done": (ctx.iteration or 0) >= 2}

    wf = _loop_wf()
    r1 = ex.run_workflow(
        wf,
        run_id="PR",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"echo": lambda c: {"echoed": "x"}, "score": flaky},
    )
    assert r1.status == "failed"
    assert calls == ["L#0/attempt", "L#1/attempt", "L#2/attempt"]  # 0,1 committed; 2 failed
    calls.append("fixed")  # the "fix": iteration 2 now succeeds on resume
    r2 = ex.run_workflow(
        wf,
        run_id="PR",
        target_ticket=tid,
        repo_root=str(rebar_repo),
        scripted_registry={"echo": lambda c: {"echoed": "x"}, "score": flaky},
    )
    assert r2.status == "succeeded"
    # Only iteration 2 re-ran on resume; 0 and 1 were skipped from their committed markers.
    assert [c for c in calls if c.startswith("L#")] == [
        "L#0/attempt",
        "L#1/attempt",
        "L#2/attempt",
        "L#2/attempt",
    ]
    assert r2.outputs["L"] == {"iterations": 3}
