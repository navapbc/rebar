"""Verify retryable completion outages retain their disposition through the raised-error path.

`close_precheck` can then select its retry path. The tests also classify botocore read and connect
timeouts without importing botocore.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError, LLMUnavailableError
from rebar.llm.failure import (
    LLMOutcome,
    ResolutionClass,
    classify_llm_failure,
    outcome_of,
    resolution_fields,
)
from rebar.llm.workflow.completion_recovery import (
    CompletionAgentStep,
    raise_completion_workflow_failure,
)
from rebar.llm.workflow.executor import RunResult, StepContext

pytestmark = pytest.mark.unit


def _retryable_outcome() -> LLMOutcome:
    return LLMOutcome(
        ResolutionClass.WAIT_AND_RETRY, {"type": "overload", "status_code": 529}, retryable=True
    )


def _failed_result(error: str) -> RunResult:
    return RunResult(
        run_id="run-1",
        workflow_name="completion-verification",
        status="failed",
        outputs={},
        terminal_step=None,
        terminal_output=None,
        error=error,
    )


# ── half 1: the raised error carries the disposition close_precheck reads ──────────────────
def test_completion_failure_carries_retryable_disposition() -> None:
    """When the finalized failure's ``failure_diagnostic`` carries a retryable disposition (the
    shape ``resolution_fields`` writes), ``raise_completion_workflow_failure`` must attach a
    matching ``LLMOutcome`` to the raised error so ``outcome_of(exc).retryable`` is True — the
    exit-11 path. This is the completion analogue of
    ``test_degraded_code_review_carries_disposition``."""
    failure_diagnostic = {
        "workflow_status": "failed",
        **resolution_fields(_retryable_outcome()),
    }
    with pytest.raises(LLMError) as excinfo:
        raise_completion_workflow_failure(
            "T-1",
            _failed_result("the LLM provider call failed: overloaded"),
            failure_diagnostic,
            0,
            None,
        )
    outcome = outcome_of(excinfo.value)
    assert outcome is not None, "the raised completion failure dropped the retryable disposition"
    assert outcome.retryable is True
    assert outcome.resolution_class is ResolutionClass.WAIT_AND_RETRY


def test_completion_failure_without_disposition_stays_outcome_less() -> None:
    """A non-outage failure (no disposition on ``failure_diagnostic``) must NOT invent one — it
    stays outcome-less so ``close_precheck`` keeps its fail-closed exit 1. The refactoring-litmus
    guard against a fix that blanket-stamps every failure retryable."""
    with pytest.raises(LLMError) as excinfo:
        raise_completion_workflow_failure(
            "T-1", _failed_result("banked no verdicts before exhausting its budget"), None, 0, None
        )
    assert outcome_of(excinfo.value) is None


# ── half 2: the producer — a mid-run provider outage records its disposition ───────────────
class _OutageRunner:
    """A primary run that dies with a RETRYABLE provider outage carrying its ``.outcome`` (as
    ``run_failure.interpret_failure`` attaches it)."""

    name = "outage"

    def preflight(self) -> None:
        return None

    def run(self, req):
        exc = LLMUnavailableError("the LLM provider call failed: read timed out")
        exc.outcome = _retryable_outcome()  # type: ignore[attr-defined]
        raise exc


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
            "context": "<untrusted_ticket_context>t</untrusted_ticket_context>",
        },
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


def test_completion_agent_step_preserves_outage_disposition(monkeypatch) -> None:
    """A retryable provider outage that propagates out of ``CompletionAgentStep.run`` must leave the
    retryable disposition on ``failure_diagnostic`` for the workflow layer to forward — currently
    it propagates raw and ``failure_diagnostic`` stays ``None`` (the dropped-disposition defect)."""
    step = CompletionAgentStep(
        runner=_OutageRunner(), repo_root=None, config=LLMConfig(runner="fake")
    )
    with pytest.raises(LLMUnavailableError):
        step.run(_ctx())
    assert step.failure_diagnostic is not None, (
        "the provider outage propagated without recording its disposition"
    )
    assert step.failure_diagnostic.get("retryable") is True
    assert step.failure_diagnostic.get("resolution_class") == ResolutionClass.WAIT_AND_RETRY.value


# Botocore read and connect timeout names are retryable only under the botocore module.
def _timeout_exc(name: str, module: str) -> BaseException:
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    return cls("read timed out")


@pytest.mark.parametrize("name", ["ReadTimeoutError", "ConnectTimeoutError"])
def test_botocore_transport_timeout_classifies_retryable(name: str) -> None:
    outcome = classify_llm_failure(_timeout_exc(name, "botocore.exceptions"))
    assert outcome.resolution_class is ResolutionClass.WAIT_AND_RETRY
    assert outcome.retryable is True


@pytest.mark.parametrize("name", ["ReadTimeoutError", "ConnectTimeoutError"])
def test_same_named_non_botocore_timeout_not_retryable(name: str) -> None:
    """Teeth for the module guard: a same-named timeout from an UNRELATED module must NOT be
    swept into the retryable arm — otherwise the arm is an unguarded name match."""
    outcome = classify_llm_failure(_timeout_exc(name, "acme.widgets"))
    assert outcome.resolution_class is not ResolutionClass.WAIT_AND_RETRY
    assert outcome.retryable is False
