"""WS-E: store-touching scripted steps + an end-to-end scripted workflow.

fetch_ticket / comment_verdict (idempotent) / tag / set_fields against a real
store, plus a fetch -> gate -> comment scripted chain via rebar.run_workflow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import schemas
from rebar.llm.workflow import steps
from rebar.llm.workflow.executor import StepContext, contract_for

pytest.importorskip("jsonschema")


def _check_output(op_name: str, output) -> None:
    """e050 per-op output-shape guard: the op's ACTUAL output validates against its
    declared OUTPUT contract schema (a StepResult is unwrapped to its outputs)."""
    out = output.outputs if hasattr(output, "outputs") else output
    schema_name = contract_for(op_name).output_schema
    schemas.validator(schema_name).validate(out)


def _ctx(repo, tid, inputs, *, run_id="R", step_id="s"):
    return StepContext(
        run_id=run_id,
        step_id=step_id,
        kind="scripted",
        step={},
        inputs=inputs,
        workflow={},
        target_ticket=tid,
        repo_root=str(repo),
    )


def test_fetch_ticket(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "My Title", description="Body", repo_root=str(rebar_repo))
    out = steps.fetch_ticket(_ctx(rebar_repo, tid, {}))
    assert out["title"] == "My Title"
    assert out["status"] == "open"
    assert out["ticket"]["ticket_id"] == tid
    _check_output("fetch_ticket", out)


def test_fetch_commits_and_epic_graph_outputs(rebar_repo: Path) -> None:
    # e050: exercise the two remaining read ops + validate their output contracts.
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    fc = steps.fetch_commits(_ctx(rebar_repo, tid, {}))
    assert fc["commit_count"] == 0 and fc["commits"] == []
    _check_output("fetch_commits", fc)
    fg = steps.fetch_epic_graph(_ctx(rebar_repo, tid, {}))
    assert set(fg) == {"graph", "children", "blockers", "deps"}
    _check_output("fetch_epic_graph", fg)


def test_comment_verdict_is_idempotent(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    ctx = _ctx(rebar_repo, tid, {"verdict": "pass", "summary": "looks good"})
    r1 = steps.comment_verdict(ctx)
    assert r1.outputs["commented"] is True
    _check_output("comment_verdict", r1)
    # Same (run_id, step_id) -> the marker is found, so no second comment.
    r2 = steps.comment_verdict(ctx)
    assert r2.outputs["commented"] is False
    assert r2.outputs["idempotent_skip"] is True
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    verdict_comments = [c for c in state["comments"] if "Workflow verdict" in (c.get("body") or "")]
    assert len(verdict_comments) == 1


def test_tag_step(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    out = steps.tag_step(_ctx(rebar_repo, tid, {"tag": "reviewed"}))
    _check_output("tag", out)
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "reviewed" in state["tags"]


def test_set_fields_step(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    out = steps.set_fields(_ctx(rebar_repo, tid, {"fields": {"priority": 0}}))
    _check_output("set_fields", out)
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["priority"] == 0


def test_end_to_end_scripted_chain(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Target", repo_root=r)
    doc = {
        "schema_version": "1",
        "name": "scripted_chain",
        "inputs": {"ticket_id": {"type": "string"}},
        "steps": [
            {
                "id": "fetch",
                "uses": "fetch_ticket",
                "with": {"ticket_id": "${{ inputs.ticket_id }}"},
            },
            {
                "id": "gate",
                "uses": "gate",
                "needs": ["fetch"],
                "with": {"findings": [], "policy": "default"},
            },
            {
                "id": "comment",
                "uses": "comment_verdict",
                "needs": ["gate"],
                "with": {
                    "ticket_id": "${{ inputs.ticket_id }}",
                    "verdict": "${{ steps.gate.outputs.verdict }}",
                },
            },
        ],
    }
    res = rebar.run_workflow(doc, {"ticket_id": tid}, ticket_id=tid, repo_root=r)
    assert res["status"] == "succeeded"
    assert res["outputs"]["gate"]["verdict"] == "pass"
    assert res["outputs"]["comment"]["commented"] is True
    state = rebar.show_ticket(tid, repo_root=r)
    assert any("Workflow verdict: pass" in (c.get("body") or "") for c in state["comments"])
