"""Contract for unknown scripted workflow steps.

``_dispatch`` raises ``WorkflowUnknownStepError`` for an unknown ``uses`` value. That subtype maps
to ``invalid_input`` through both ``error_code_for`` and ``_structured_llm_failure``. The bare
``WorkflowError`` and provider outages remain ``llm_unavailable``. The bare ``LLMError`` maps to
``command_failed``.
"""

from __future__ import annotations

import pytest


def _unknown_step_ctx():
    from rebar.llm.workflow.executor import StepContext

    return StepContext(
        run_id="r1",
        step_id="s1",
        kind="scripted",
        step={"uses": "__definitely_not_a_real_step__"},
        inputs={},
        workflow={},
    )


# ── AC1: the executor unknown-step SITE raises the precise subtype (message preserved) ───
def test_dispatch_unknown_step_raises_unknown_step_error() -> None:
    from rebar.llm.errors import WorkflowUnknownStepError
    from rebar.llm.workflow.executor import _dispatch

    with pytest.raises(WorkflowUnknownStepError) as ei:
        _dispatch(_unknown_step_ctx(), {}, None)
    assert "unknown scripted step" in str(ei.value)
    assert "__definitely_not_a_real_step__" in str(ei.value)


# ── AC1: that subtype classifies as invalid_input via error_code_for ─────────────────────
def test_unknown_step_error_classifies_as_invalid_input() -> None:
    import rebar
    from rebar.llm.errors import WorkflowUnknownStepError

    code = rebar.error_code_for(WorkflowUnknownStepError("unknown scripted step 'x' (not in ...)"))
    assert code == "invalid_input"
    assert code != "llm_unavailable"
    assert code in rebar.KNOWN_ERROR_CODES


# ── AC1: and through the LLM-tier surface (_structured_llm_failure) ──────────────────────
def test_unknown_step_error_structured_llm_failure_is_invalid_input() -> None:
    import rebar
    from rebar._mcp_llm import _structured_llm_failure
    from rebar.llm.errors import WorkflowUnknownStepError

    out = _structured_llm_failure(
        WorkflowUnknownStepError("unknown scripted step 'x' (not in ...)")
    )
    assert out["error"] == "invalid_input"
    assert out["error"] != "llm_unavailable"
    assert out["error"] in rebar.KNOWN_ERROR_CODES


# ── AC2/AC3: the execute base + genuine outages STILL map to llm_unavailable ──────────────
def test_execute_base_and_outages_still_llm_unavailable() -> None:
    import rebar
    from rebar.llm.errors import LLMError, LLMUnavailableError, WorkflowError

    assert rebar.error_code_for(WorkflowError("execute step failed: provider overloaded")) == (
        "llm_unavailable"
    )
    assert rebar.error_code_for(LLMUnavailableError("no API key")) == "llm_unavailable"
    assert rebar.error_code_for(LLMError("generic llm failure")) == "command_failed"


# ── AC4: WorkflowUnknownStepError is a WorkflowError subtype (inherits the engine tree) ───
def test_unknown_step_error_is_workflow_error_subtype() -> None:
    from rebar.llm.errors import WorkflowError, WorkflowUnknownStepError

    assert issubclass(WorkflowUnknownStepError, WorkflowError)
