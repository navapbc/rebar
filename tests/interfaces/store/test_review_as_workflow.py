"""WS-K2: review_ticket reframed as a workflow + parallel-run-and-diff.

Proves (a) the RunnerAgentStep bridge runs a real agent workflow through the
executor (closing the WS-C4 _PendingAgentRunner gap), and (b) the workflow path is
equivalent (same findings/summary) to the legacy review_ticket — the cutover gate.
The public review_ticket entry point is unchanged (legacy path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm import review_workflows as RW
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


def test_review_workflow_equivalent_to_legacy(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Diff me", description="some body", repo_root=r)
    fake = FakeRunner(
        [{"severity": "high", "dimension": "x", "detail": "d"}], summary="the summary"
    )
    diff = RW.diff_review_paths(tid, "code-quality", repo_root=r, runner=fake)
    assert diff["equivalent"] is True, diff
    assert diff["legacy_findings"] == diff["workflow_findings"]
    assert diff["legacy_summary"] == diff["workflow_summary"] == "the summary"


def test_public_review_ticket_entry_point_unchanged(rebar_repo: Path) -> None:
    # The documented entry point still works via the legacy path (not deleted/cut over).
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Legacy", repo_root=r)
    result = rebar.llm.review_ticket(tid, "code-quality", runner=FakeRunner([]), repo_root=r)
    assert result["runner"] == "fake"
    assert result["target"]["kind"] == "ticket"  # legacy provenance preserved
