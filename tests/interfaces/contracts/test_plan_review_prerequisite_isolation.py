from __future__ import annotations

import json
from types import SimpleNamespace

from rebar.llm.plan_review import orchestrator, passes
from rebar.llm.plan_review.prerequisites import focused_inputs
from rebar.llm.plan_review.workflow_ops import plan_review_coach_inputs
from rebar.llm.workflow.executor import StepContext


def test_coach_prompt_inputs_exclude_prerequisite_identity_and_plan(monkeypatch) -> None:
    prerequisite_id = "aaaa-bbbb-cccc-dddd"
    prerequisite_plan = "PRIVATE PREREQUISITE PLAN TEXT"
    monkeypatch.setattr(
        orchestrator,
        "assemble_context",
        lambda *args, **kwargs: SimpleNamespace(plan_text="subject plan"),
    )
    monkeypatch.setattr(passes, "load_move_registry", lambda repo_root: {})
    monkeypatch.setattr(passes, "applicable_moves", lambda moves, triggers: {})
    monkeypatch.setattr(
        passes,
        "coach_instructions",
        lambda findings, moves: json.dumps(findings, sort_keys=True),
    )
    ctx = StepContext(
        run_id="run",
        step_id="coach-inputs",
        kind="scripted",
        step={},
        inputs={
            "surviving": [],
            "blocking": [
                {
                    "id": "finding-id",
                    "finding": f"{prerequisite_id} contradicts {prerequisite_plan}",
                    "criteria": ["prerequisite-consistency"],
                    "prerequisite_id": prerequisite_id,
                    "evidence": [prerequisite_plan],
                }
            ],
            "indeterminate": [],
            "prerequisite_coverage": [
                {
                    "prerequisite_id": prerequisite_id,
                    "disposition": "finding",
                    "findings": [],
                }
            ],
        },
        workflow={},
        target_ticket="subject-ticket",
    )

    with focused_inputs([{"canonical_id": prerequisite_id, "rendered_text": prerequisite_plan}]):
        result = plan_review_coach_inputs(ctx)

    prompt_payload = result["instructions"]
    assert prerequisite_id not in prompt_payload
    assert prerequisite_plan not in prompt_payload
    assert "[direct prerequisite]" in prompt_payload
