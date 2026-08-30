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
"""

from __future__ import annotations

import rebar
from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError


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
