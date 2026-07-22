"""Schema/YAML cutover contract for execution-phase Pass-3."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def test_phase_is_required_by_both_workflow_contracts() -> None:
    precheck = json.loads(
        (ROOT / "src/rebar/schemas/plan_review_precheck_output.schema.json").read_text()
    )
    decide = json.loads(
        (ROOT / "src/rebar/schemas/plan_review_decide_input.schema.json").read_text()
    )
    enum = {"type": "string", "enum": ["planning", "execution"]}
    for schema in (precheck, decide):
        assert {key: schema["properties"]["review_phase"][key] for key in ("type", "enum")} == enum
        assert "review_phase" in schema["required"]
    assert precheck["additionalProperties"] is False
    assert decide["additionalProperties"] is True


def test_both_decide_branches_receive_precheck_phase() -> None:
    workflow = yaml.safe_load((ROOT / "src/rebar/llm/workflow/gates/plan-review.yaml").read_text())
    decide_steps = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("uses") == "plan_review_decide":
                decide_steps.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(workflow)
    assert len(decide_steps) == 2
    assert {step["with"]["review_phase"] for step in decide_steps} == {
        "${{ steps.precheck.outputs.review_phase }}"
    }
