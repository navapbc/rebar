"""Production scripted-step proofs for execution-phase Pass-3 routing."""

from __future__ import annotations

from types import SimpleNamespace

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import orchestrator, workflow_ops
from rebar.llm.workflow import gate_dispatch
from rebar.llm.workflow.executor import StepContext


def _finding() -> dict:
    return {
        "criteria": ["T1"],
        "finding": "specific defect",
        "suggested_fix": "fix it",
        "citations": [],
        "evidence": [],
        "scenarios": [],
    }


def _verification() -> dict:
    return {
        "index": 0,
        "severity_attributes": {
            "internal_conflict": "medium",
            "silent_vs_self_revealing": "silent",
        },
        "binary": {
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
    }


def _decide(phase: str) -> dict:
    return workflow_ops.plan_review_decide(
        StepContext(
            run_id="r",
            step_id="decide",
            kind="scripted",
            step={},
            inputs={
                "findings": [_finding()],
                "verifications": [_verification()],
                "det_blocking": [],
                "det_advisory": [],
                "review_phase": phase,
            },
            workflow={},
            target_ticket="1111-2222-3333-4444",
            repo_root=None,
        )
    )


def test_decide_step_applies_execution_floor_but_planning_does_not(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator._criteria,
        "threshold_for",
        lambda criteria, descriptors, *, gate: (0.65, True),
    )
    planning = _decide("planning")
    execution = _decide("execution")
    assert [item["decision"] for item in planning["blocking"]] == ["block"]
    assert execution["blocking"] == []
    assert execution["surfaced"][0]["finding"] == "specific defect"


def test_verify_failure_recovery_threads_phase_and_legacy_defaults_planning(monkeypatch) -> None:
    original = orchestrator.pass3_over_findings
    seen: list[bool] = []

    def capture(*args, execution_review=False, **kwargs):
        seen.append(execution_review)
        return original(*args, execution_review=execution_review, **kwargs)

    monkeypatch.setattr(orchestrator, "pass3_over_findings", capture)

    def recover(precheck: dict) -> dict:
        rec = SimpleNamespace(
            steps=[
                {"status": "succeeded", "frame_key": "precheck", "outputs": precheck},
                {
                    "status": "succeeded",
                    "frame_key": "review/then/finders",
                    "outputs": {"findings": [{"finding": "uses A1", "criteria": ["A1"]}]},
                },
            ]
        )
        return gate_dispatch._recover_plan_review_verify_failure(
            rec, LLMConfig(runner="fake"), error="verify failed"
        )

    base = {
        "det_coverage": {},
        "det_blocking": [],
        "det_advisory": [],
        "canonical_id": "1111-2222-3333-4444",
        "ticket_type": "task",
    }
    assert recover({**base, "review_phase": "execution"}) is not None
    assert recover(base) is not None
    assert seen == [True, False]
