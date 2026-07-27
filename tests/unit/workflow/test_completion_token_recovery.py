"""Visible contract tests for bounded completion-verifier recovery."""

from __future__ import annotations

import json

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import CompletionRecoveryError, UnretryableOutputError
from rebar.llm.workflow.completion_recovery import (
    CompletionAgentStep,
    explicit_completion_criteria,
)
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit


def _ctx() -> StepContext:
    return StepContext(
        run_id="run-1",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "completion-verifier",
            "mode": "structured",
            "output_schema": "completion_verdict",
        },
        inputs={
            "ticket_id": "T-1",
            "context": "<untrusted_ticket_context>ticket</untrusted_ticket_context>",
        },
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


def _ticket() -> dict:
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, 7))
    return {
        "ticket_id": "T-1",
        "title": "bounded completion",
        "ticket_type": "bug",
        "description": f"## Acceptance Criteria\n{criteria}",
    }


def test_bug_criteria_append_core_resolution_after_explicit_acceptance_criteria() -> None:
    criteria = explicit_completion_criteria(_ticket())

    assert criteria[:6] == [f"criterion {index}" for index in range(1, 7)]
    assert criteria[6] == (
        "Bug 'bounded completion' is actually resolved: the reported defect no longer "
        "reproduces and expected behavior holds."
    )


def test_non_bug_without_explicit_criteria_fails_closed() -> None:
    with pytest.raises(CompletionRecoveryError, match="cannot enumerate"):
        explicit_completion_criteria(
            {
                "ticket_id": "T-2",
                "title": "vague task",
                "ticket_type": "task",
                "description": "Improve the workflow when appropriate.",
            }
        )


class _RecoverableRunner:
    name = "recoverable"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        if len(self.requests) == 1:
            raise UnretryableOutputError("finish_reason=length")
        if req.execution_mode == "agentic":
            assert req.mode == "text"
            assert req.tool_step_limit == 16
            assert req.iteration_limit == 40
            return {"text": "Observed implementation evidence at src/example.py:10."}

        assert req.execution_mode == "single_turn"
        payload = json.loads(req.instructions)
        criteria = [
            {
                "criterion": criterion,
                "met": True,
                "citation": {
                    "kind": "source",
                    "description": "src/example.py:10",
                },
                "kind": "codebase-verifiable",
            }
            for criterion in payload["expected_criteria"]
        ]
        return {"verdict": "PASS", "findings": [], "criteria": criteria}


def test_aggregate_truncation_recovers_with_bounded_evidence_and_fresh_finalizer(
    monkeypatch,
) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _RecoverableRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    result = step.run(_ctx()).outputs

    assert result["verdict"] == "PASS"
    assert [record["criterion"] for record in result["criteria"]] == [
        *(f"criterion {index}" for index in range(1, 7)),
        (
            "Bug 'bounded completion' is actually resolved: the reported defect no longer "
            "reproduces and expected behavior holds."
        ),
    ]
    assert len(runner.requests) == 9  # aggregate + seven evidence + finalizer
    assert runner.requests[-1].execution_mode == "single_turn"
    assert runner.requests[-1].tool_step_limit is None


class _SimpleRunner:
    name = "simple"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        return {"verdict": "PASS", "findings": [], "criteria": []}


def test_simple_completion_keeps_one_call_fast_path() -> None:
    runner = _SimpleRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    assert step.run(_ctx()).outputs["verdict"] == "PASS"
    assert len(runner.requests) == 1


class _ExhaustedRunner(_RecoverableRunner):
    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        raise UnretryableOutputError("finish_reason=length")


def test_recovery_exhaustion_stays_typed_and_actionable(monkeypatch) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _ExhaustedRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    with pytest.raises(CompletionRecoveryError) as caught:
        step.run(_ctx())

    message = str(caught.value)
    assert "increasing max_tokens alone cannot repair" in message
    assert "raise max_tokens" not in message
    assert step.failure_diagnostic is not None
    assert step.failure_diagnostic["stage"] == "evidence"
    assert step.failure_diagnostic["criteria_completed"] == 0


class _RefusalRunner(_RecoverableRunner):
    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        raise UnretryableOutputError("the model refused to answer")


def test_non_exhaustion_unretryable_error_does_not_enter_recovery() -> None:
    runner = _RefusalRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    with pytest.raises(UnretryableOutputError, match="refused"):
        step.run(_ctx())

    assert len(runner.requests) == 1


class _ContradictoryFinalizerRunner:
    name = "contradictory-finalizer"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        if len(self.requests) == 1:
            raise UnretryableOutputError("finish_reason=length")
        if req.execution_mode == "agentic":
            return {"text": "Repository evidence was gathered."}

        payload = json.loads(req.instructions)
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [
                {
                    "criterion": criterion,
                    "met": index != len(payload["expected_criteria"]) - 1,
                    "evidence": ["src/example.py:10"],
                    "kind": "codebase-verifiable",
                }
                for index, criterion in enumerate(payload["expected_criteria"])
            ],
            "summary": "Contradictory PASS.",
        }


def test_recovery_rejects_pass_when_any_exact_criterion_is_unmet(monkeypatch) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _ContradictoryFinalizerRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    with pytest.raises(CompletionRecoveryError, match="unmet criterion"):
        step.run(_ctx())


@pytest.mark.parametrize(
    "ticket",
    [
        {
            "ticket_id": "T-many",
            "title": "too many criteria",
            "ticket_type": "task",
            "description": "## Acceptance Criteria\n"
            + "\n".join(f"- [ ] criterion {index}" for index in range(1, 34)),
        },
        {
            "ticket_id": "T-large",
            "title": "oversized criterion",
            "ticket_type": "task",
            "description": "## Acceptance Criteria\n- [ ] " + ("x" * 4_001),
        },
    ],
)
def test_unbounded_criteria_fail_before_any_recovery_call(monkeypatch, ticket) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: ticket)
    runner = _RecoverableRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    with pytest.raises(CompletionRecoveryError, match="bound"):
        step.run(_ctx())

    assert len(runner.requests) == 1


def test_unbounded_ticket_context_fails_before_any_recovery_call(monkeypatch) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _RecoverableRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )
    ctx = _ctx()
    ctx.inputs["context"] = "x" * 24_001

    with pytest.raises(CompletionRecoveryError, match="context.*bound"):
        step.run(ctx)

    assert len(runner.requests) == 1


class _MetadataLengthRunner(_RecoverableRunner):
    def run(self, req):  # noqa: ANN001, ANN201
        if not self.requests:
            self.requests.append(req)
            exc = UnretryableOutputError("provider stopped")
            exc.diagnostic = {"finish_reason": "length"}
            raise exc
        return super().run(req)


def test_metadata_only_length_enters_bounded_recovery(monkeypatch) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _MetadataLengthRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    assert step.run(_ctx()).outputs["verdict"] == "PASS"
    assert len(runner.requests) > 1


class _ContextRefusalRunner:
    name = "context-refusal"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        raise UnretryableOutputError("refused because context policy was triggered")


def test_non_length_error_containing_context_does_not_enter_recovery() -> None:
    runner = _ContextRefusalRunner()
    step = CompletionAgentStep(
        runner=runner,
        repo_root=None,
        config=LLMConfig(runner="fake"),
    )

    with pytest.raises(UnretryableOutputError, match="context policy"):
        step.run(_ctx())

    assert len(runner.requests) == 1
