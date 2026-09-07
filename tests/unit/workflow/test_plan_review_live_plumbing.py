"""Exercises plan-review prompt plumbing through ``RunnerAgentStep``.

Verify and coach prompts require plan data beyond the generic ticket fields. These offline
tests ensure the workflow supplies those variables so runtime prompt resolution succeeds.
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
    """The verify step must supply its ``shared_prefix`` input to prompt resolution."""
    from rebar.llm.prompting import prompts

    step = {
        "id": "verify",
        "prompt": "plan-review-verifier",
        "mode": "structured",
        "output_schema": "plan_review_verification",
    }
    prefix = prompts.shared_plan_prefix("## Plan\nBuild X in src/x.py.")
    ctx = _ctx(step, {"ticket_id": "T-1", "shared_prefix": prefix, "findings": []})
    runner = RunnerAgentStep(runner=FakeRunner(structured={"verifications": []}), repo_root=None)
    res = runner.run(ctx)  # must NOT raise PromptError — {{shared_prefix}} resolved from with
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
