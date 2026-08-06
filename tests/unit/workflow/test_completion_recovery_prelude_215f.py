"""Held-out oracle for bug 215f: a `_recover` PRELUDE failure must not discard the
primary run's diagnostic.

The defect: `_reads.show_ticket` runs before the `try:` whose `except` arm is the only
place `self.failure_diagnostic` is built. A `RebarError` there (missing id, store fails
to reduce) escapes `_recover` raw: the workflow layer flattens it with
`failure_diagnostic` still None, so no gate_error_v1 sidecar and the primary run's
already-computed budget/repetition diagnostic is dropped in full — total evidence loss
on a narrow trigger, on a gate that SIGNS.
"""

from __future__ import annotations

import pytest

from rebar._errors import RebarError
from rebar.llm.config import LLMConfig
from rebar.llm.errors import CompletionRecoveryError, LLMBudgetExhaustedError
from rebar.llm.workflow.completion_recovery import (
    CompletionAgentStep,
    raise_completion_workflow_failure,
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


class _BudgetExhaustingRunner:
    """Primary run dies at the step budget carrying the repetition diagnostic."""

    name = "budget-exhausting"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):  # noqa: ANN001, ANN201
        self.requests.append(req)
        err = LLMBudgetExhaustedError("agent exceeded its step budget")
        err.diagnostic = {
            "requests": 240,
            "tool_calls": 247,
            "tool_calls_distinct": 42,
            "max_consecutive_repeat": 1,
            "distinct_ratio_window": 0.208,
            "top_repeated_tool_calls": [{"signature": "search_files:4228f20a", "count": 43}],
        }
        raise err


def _run_with_broken_prelude(monkeypatch) -> tuple[CompletionAgentStep, BaseException]:
    def _missing(*args, **kwargs):
        raise RebarError("no ticket found for id 'T-1'")

    monkeypatch.setattr("rebar._reads.show_ticket", _missing)
    step = CompletionAgentStep(
        runner=_BudgetExhaustingRunner(), repo_root=None, config=LLMConfig(runner="fake")
    )
    with pytest.raises(Exception) as excinfo:
        step.run(_ctx())
    return step, excinfo.value


def test_prelude_rebar_error_stays_typed_not_raw(monkeypatch) -> None:
    """The prelude failure must surface as the recovery-failure contract type, not a
    raw RebarError that the workflow layer flattens diagnostic-less."""
    _, raised = _run_with_broken_prelude(monkeypatch)

    assert isinstance(raised, CompletionRecoveryError), (
        f"a prelude failure escaped _recover as raw {type(raised).__name__}; "
        "the primary diagnostic is dropped on this path"
    )


def test_prelude_failure_preserves_primary_aggregate_diagnostic(monkeypatch) -> None:
    step, raised = _run_with_broken_prelude(monkeypatch)

    diagnostic = step.failure_diagnostic
    assert isinstance(diagnostic, dict) and diagnostic, (
        "failure_diagnostic must be populated so raise_completion_workflow_failure "
        "emits the sidecar"
    )
    assert diagnostic.get("aggregate_distinct_ratio_window") == 0.208
    assert diagnostic.get("aggregate_max_consecutive_repeat") == 1
    assert diagnostic.get("aggregate_top_repeated_tool_calls") == [
        {"signature": "search_files:4228f20a", "count": 43}
    ]
    # The failure is classified to the stage that failed, before any evidence run.
    assert diagnostic.get("stage") == "preflight"
    assert diagnostic.get("criteria_completed") == 0


def test_prelude_failure_emits_gate_error_sidecar(monkeypatch) -> None:
    step, _ = _run_with_broken_prelude(monkeypatch)

    captured: dict = {}

    def _capture(ticket_id, gate, **kwargs):
        captured["ticket_id"] = ticket_id
        captured["gate"] = gate
        captured.update(kwargs)

    monkeypatch.setattr("rebar.llm.gate_error_sidecar.emit_gate_error", _capture)

    from rebar.llm.workflow.executor import RunResult

    with pytest.raises(CompletionRecoveryError):
        raise_completion_workflow_failure(
            "T-1",
            RunResult(
                run_id="run-1",
                workflow_name="completion-verification",
                status="failed",
                outputs={},
                terminal_step="verify",
                terminal_output=None,
                error="completion LLM tier failed",
            ),
            step.failure_diagnostic,
            workflow_steps_recorded=1,
            repo_root=None,
        )

    assert captured.get("ticket_id") == "T-1"
    diagnostic = captured.get("diagnostic") or {}
    assert diagnostic.get("aggregate_distinct_ratio_window") == 0.208
    assert diagnostic.get("aggregate_top_repeated_tool_calls")


def test_prelude_failure_names_the_prelude_not_token_growth(monkeypatch) -> None:
    """The operator remedy for a missing ticket is nothing like the remedy for token
    exhaustion; the message must not claim the aggregate history was exhausted."""
    _, raised = _run_with_broken_prelude(monkeypatch)

    message = str(raised)
    assert "no ticket found" in message, "the prelude cause must be named"
    assert "max_tokens" not in message, (
        "a prelude failure must not be explained as token-history growth"
    )
