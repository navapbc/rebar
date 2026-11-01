"""Happy-path oracle for f75f-5509-10c0-4430.

The blanket ``LLMError -> llm_unavailable`` fallthrough in ``error_code_for`` mislabels the
deliberately-non-availability ``LLMRunnerError`` subtree (input-rejection, budget/tool-loop
self-stops, context-window, output defects) as a provider outage. Those exception types'
own docstrings forbid the outage framing ("must never surface as 'the LLM provider call
failed'"). This oracle pins the corrected taxonomy on the OBSERVABLE, public
``rebar.error_code_for`` contract:

- a ``LLMRunnerError`` (here the headline ``LLMInputRejectedError``) -> ``command_failed``
  (the honest broad code — makes no false availability claim), and
- a genuine ``LLMUnavailableError`` -> ``llm_unavailable`` stays RESERVED (unchanged).

The dbca-97ac-ad96-4d6d workflow taxonomy (``not_found``/``invalid_input``) and the bare
``WorkflowError``/``LLMError`` catch-all are deliberately UNTOUCHED by this change; those
invariants live in the held-out oracle.

Held-out oracles merged from test_llmrunner_error_codes_f75f_heldout: Contract for runner error
classification.
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


def test_input_rejected_is_command_failed_not_an_outage() -> None:
    # The provider ANSWERED and rejected the input; nothing is unavailable. It must not
    # claim an outage a caller could wait out.
    code = rebar.error_code_for(LLMInputRejectedError("prompt too large"))
    assert code == "command_failed"
    assert code != "llm_unavailable"
    assert code in rebar.KNOWN_ERROR_CODES


def test_genuine_unavailability_stays_llm_unavailable() -> None:
    # The reserved code is preserved for a real availability fault.
    code = rebar.error_code_for(LLMUnavailableError("no API key / provider unreachable"))
    assert code == "llm_unavailable"
    assert code in rebar.KNOWN_ERROR_CODES


# ── held-out oracles (merged from test_llmrunner_error_codes_f75f_heldout) ──

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
