"""Cover the ``RunnerAgentStep`` bridge.

The bridge lets a workflow step run an agent through the executor, so a review-shaped
step reaches a runner without the step itself knowing how an agent is driven.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import runs

pytest.importorskip("jsonschema")


def test_runner_agent_step_bridge_runs_an_agent_workflow(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Bridge", description="body", repo_root=r)
    doc = {
        "schema_version": "1",
        "name": "agent_demo",
        "steps": [
            {
                "id": "review",
                "prompt": "code-quality",
                "mode": "findings",
                "output_schema": "review_result",
                "with": {"ticket_id": tid, "context": "clean context"},
            }
        ],
    }
    fake = FakeRunner([{"severity": "medium", "dimension": "bugs", "detail": "z"}], summary="ok")
    res = runs.run(doc, {}, repo_root=r, review_runner=fake)
    assert res["status"] == "succeeded", res
    out = res["terminal_output"]
    assert out["findings"][0]["detail"] == "z"
    assert out["summary"] == "ok"
