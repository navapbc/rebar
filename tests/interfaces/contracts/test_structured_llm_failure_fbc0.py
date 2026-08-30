"""Contract oracle for fbc0-ee15-9d69-49ae.

``_structured_llm_failure`` is the shared failure-envelope helper the four
``REBAR_MCP_ALLOW_LLM`` gate tools (``review_code``, ``scan_spec``,
``verify_completion``, ``review_plan``) route their ``except LLMError`` arm through.
It must NOT hardcode ``error="llm_unavailable"``; it must DELEGATE the code to the
shared ``error_code_for`` classifier so the whole taxonomy surfaces uniformly through
the gate tools:

- a genuine outage stays ``llm_unavailable`` (RESERVED),
- an ``LLMRunnerError`` self-inflicted / input / output fault -> ``command_failed``
  (rides f75f-5509-10c0-4430), and
- a workflow caller-input / not-found error -> ``not_found`` / ``invalid_input``
  (rides dbca-97ac-ad96-4d6d).

The distinct-code assertion is the anti-regression teeth: a hardcoded constant would
collapse every row to one code.
"""

from __future__ import annotations

import pytest

from rebar._mcp_llm import _structured_llm_failure
from rebar.llm.errors import (
    LLMConfigError,
    LLMInputRejectedError,
    LLMUnavailableError,
    RunawayToolLoopError,
    WorkflowNotFoundError,
    WorkflowParseError,
)

_CASES = [
    (LLMUnavailableError("provider unreachable"), "llm_unavailable"),
    (LLMConfigError("agents extra absent"), "llm_unavailable"),
    (LLMInputRejectedError("prompt too large"), "command_failed"),
    (RunawayToolLoopError("repeating tool cycle"), "command_failed"),
    (WorkflowNotFoundError("no such workflow"), "not_found"),
    (WorkflowParseError("bad yaml"), "invalid_input"),
]


@pytest.mark.parametrize(("exc", "expected"), _CASES, ids=[type(exc).__name__ for exc, _ in _CASES])
def test_gate_envelope_delegates_error_code(exc: Exception, expected: str) -> None:
    envelope = _structured_llm_failure(exc)
    assert envelope["error"] == expected
    assert envelope["message"] == str(exc)


def test_gate_envelope_is_not_a_hardcoded_constant() -> None:
    # If the helper hardcoded a single code, every distinct exception type would
    # collapse to it. Delegation to error_code_for yields a spectrum.
    codes = {_structured_llm_failure(exc)["error"] for exc, _ in _CASES}
    assert codes == {"llm_unavailable", "command_failed", "not_found", "invalid_input"}
