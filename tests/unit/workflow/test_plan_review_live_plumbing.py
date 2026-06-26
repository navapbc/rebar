"""LIVE-path plumbing for the workflow plan-review gate (tepid-bus-pomp).

The B2 workflow plan-review tests injected CANNED agents that never call
``prompts.resolve_prompt`` — so the fact that the verify/coach prompt steps reference
``{{plan}}`` (and need the findings/surviving listing in their instructions), while the
generic ``RunnerAgentStep`` supplies only ``ticket_id``/``ticket_context``/``repo_path``,
was never exercised. The B5 cutover made the workflow path the default and review-plan
degraded to INDETERMINATE on the live path (``prompt references undefined variable(s)
['plan']``). These tests drive the verify/coach steps through the REAL ``RunnerAgentStep``
(offline, no tokens) so the live prompt resolution is exercised in CI.
"""

from __future__ import annotations

import pytest

from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit


def _ctx(step: dict, inputs: dict) -> StepContext:
    return StepContext(
        run_id="r",
        step_id=step["id"],
        kind="agent",
        step=step,
        inputs=inputs,
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )


def test_runner_agent_step_resolves_plan_for_verify() -> None:
    """The workflow plan-review VERIFY prompt uses ``{{plan}}``; RunnerAgentStep must supply
    it from the step's ``with: {plan: ...}`` so the LIVE path resolves (it raised
    ``PromptError: undefined variable ['plan']`` before tepid-bus-pomp)."""
    step = {
        "id": "verify",
        "prompt": "plan-review-verifier",
        "mode": "structured",
        "output_schema": "plan_review_verification",
    }
    ctx = _ctx(step, {"ticket_id": "T-1", "plan": "## Plan\nBuild X in src/x.py.", "findings": []})
    runner = RunnerAgentStep(runner=FakeRunner(structured={"verifications": []}), repo_root=None)
    res = runner.run(ctx)  # must NOT raise PromptError — {{plan}} resolved from with.plan
    assert res.status == "succeeded"


def test_runner_agent_step_resolves_plan_for_coach() -> None:
    """The Pass-4 COACH prompt also uses ``{{plan}}`` — same supply requirement."""
    step = {
        "id": "coach_notes",
        "prompt": "plan-review-coach",
        "mode": "structured",
        "output_schema": "plan_review_coach",
    }
    ctx = _ctx(step, {"ticket_id": "T-1", "plan": "## Plan\nBuild X.", "surviving": []})
    runner = RunnerAgentStep(runner=FakeRunner(structured={"notes": []}), repo_root=None)
    res = runner.run(ctx)
    assert res.status == "succeeded"
