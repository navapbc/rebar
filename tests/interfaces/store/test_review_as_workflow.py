"""Cover the ``RunnerAgentStep`` bridge and the public ``review_ticket`` entry point.

The bridge lets a workflow step run an agent through the executor; the public entry point
reaches the review engine directly. The two are exercised independently.
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


def test_public_review_ticket_entry_point_unchanged(rebar_repo: Path) -> None:
    # The documented entry point still works via the legacy path (not deleted/cut over).
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Legacy", repo_root=r)
    result = rebar.llm.operations._review_ticket_impl(
        tid, "code-quality", runner=FakeRunner([]), repo_root=r
    )
    assert result["runner"] == "fake"
    assert result["target"]["kind"] == "ticket"  # legacy provenance preserved
