"""Contract for runner error classification.

Every ``LLMRunnerError`` subtype maps to ``command_failed``. Availability errors map to
``llm_unavailable``. Workflow lookup errors map to ``not_found``, and workflow input errors map to
``invalid_input``. The bare ``WorkflowError`` retains ``llm_unavailable``, while the bare
``LLMError`` maps to ``command_failed``. Tests also verify these codes through
``_structured_llm_failure``.
"""

from __future__ import annotations

import pytest

import rebar
from rebar._mcp_llm import _structured_llm_failure
from rebar.llm.errors import (
    CompletionRecoveryError,
    ContextWindowExceededError,
    LLMBudgetExhaustedError,
    LLMConfigError,
    LLMError,
    LLMInputRejectedError,
    LLMRunnerError,
    LLMUnavailableError,
    RunawayToolLoopError,
    StructuredOutputError,
    UnretryableOutputError,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowParseError,
    WorkflowUnknownStepError,
    WorkflowValidationError,
    WorkflowVersionError,
)

_RUNNER_SUBTREE = [
    LLMRunnerError("runner failed"),
    LLMBudgetExhaustedError("hit step budget"),
    ContextWindowExceededError("no candidate model fits"),
    LLMInputRejectedError("prompt too large"),
    RunawayToolLoopError("repeating tool cycle"),
    StructuredOutputError("no validated findings"),
    UnretryableOutputError("truncated turn"),
    CompletionRecoveryError("primary + recovery both failed"),
]


@pytest.mark.parametrize("exc", _RUNNER_SUBTREE, ids=lambda e: type(e).__name__)
def test_every_runner_error_is_command_failed(exc: LLMRunnerError) -> None:
    code = rebar.error_code_for(exc)
    assert code == "command_failed", f"{type(exc).__name__} must be command_failed, got {code}"
    assert code in rebar.KNOWN_ERROR_CODES


@pytest.mark.parametrize(
    "exc",
    [LLMUnavailableError("provider unreachable"), LLMConfigError("agents extra absent")],
    ids=lambda e: type(e).__name__,
)
def test_availability_faults_stay_reserved(exc: LLMUnavailableError) -> None:
    assert rebar.error_code_for(exc) == "llm_unavailable"


def test_dbca_workflow_taxonomy_preserved() -> None:
    # These finer, accurate codes were landed by dbca-97ac-ad96-4d6d and MUST NOT be
    # collapsed back into a broad code by this change.
    assert rebar.error_code_for(WorkflowNotFoundError("no such workflow")) == "not_found"
    for exc in (
        WorkflowParseError("bad yaml"),
        WorkflowValidationError(["missing field"]),
        WorkflowVersionError("schema too new"),
        WorkflowUnknownStepError("uses: unknown step"),
    ):
        assert rebar.error_code_for(exc) == "invalid_input", type(exc).__name__


def test_bare_workflow_base_stays_llm_unavailable_bare_llm_base_is_command_failed() -> None:
    # dbca's execute-base compatibility stays reserved, but ce6b removes the generic
    # bare-LLMError catch-all so an unspecified framework failure no longer claims an outage.
    assert rebar.error_code_for(WorkflowError("execute-time outage")) == "llm_unavailable"
    assert rebar.error_code_for(LLMError("unspecified llm failure")) == "command_failed"


def test_input_rejected_surfaces_as_command_failed_through_gate_envelope() -> None:
    # E2E through the real MCP gate-tool failure envelope (the 4 gate tools route their
    # ``except LLMError`` arm through this helper).
    envelope = _structured_llm_failure(LLMInputRejectedError("prompt too large"))
    assert envelope["error"] == "command_failed"
    assert envelope["message"] == "prompt too large"


def test_outage_still_surfaces_as_llm_unavailable_through_gate_envelope() -> None:
    envelope = _structured_llm_failure(LLMUnavailableError("provider unreachable"))
    assert envelope["error"] == "llm_unavailable"
