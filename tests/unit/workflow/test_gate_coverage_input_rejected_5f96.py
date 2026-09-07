"""Tests distinct degradation signals for LLM availability and input rejection.

``coverage.llm_unavailable`` marks retryable availability faults. Deterministic
``LLMInputRejectedError`` failures instead set ``coverage.input_rejected`` so consumers fail
closed without treating them as outages. This happy-path oracle covers
``degrade_cause_flags`` and the code-review degrade builder. Companion tests cover edge,
end-to-end, and reader behavior.
"""

from __future__ import annotations

import pytest

from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError

pytestmark = pytest.mark.unit


def test_degrade_cause_flags_splits_availability_from_input_rejection() -> None:
    """The single writer of the cause flags: a genuine outage flags ``llm_unavailable`` (and
    NOT ``input_rejected``); a deterministic ``LLMInputRejectedError`` flags ``input_rejected``
    (and NOT ``llm_unavailable``). Exactly one is True — they are mutually exclusive."""
    from rebar.llm.failure import degrade_cause_flags

    assert degrade_cause_flags(LLMUnavailableError("overloaded")) == {
        "llm_unavailable": True,
        "input_rejected": False,
    }
    assert degrade_cause_flags(LLMInputRejectedError("prompt too large")) == {
        "llm_unavailable": False,
        "input_rejected": True,
    }


def test_degraded_code_review_verdict_flags_input_rejected_distinctly() -> None:
    """The code-review degrade builder must carry the SPLIT signal: an input-rejection degrade
    sets ``input_rejected`` True / ``llm_unavailable`` False (still an INDETERMINATE, never a
    hollow PASS), while a plain outage keeps the classic ``llm_unavailable`` True /
    ``input_rejected`` False."""
    from rebar.llm.workflow.gate_dispatch import _degraded_code_review_verdict

    rejected = _degraded_code_review_verdict(
        error=LLMInputRejectedError("prompt too large"), runner_name="r"
    )
    assert rejected["verdict"] == "INDETERMINATE"
    assert rejected["coverage"]["input_rejected"] is True
    assert rejected["coverage"]["llm_unavailable"] is False

    outage = _degraded_code_review_verdict(error=LLMUnavailableError("overloaded"), runner_name="r")
    assert outage["verdict"] == "INDETERMINATE"
    assert outage["coverage"]["llm_unavailable"] is True
    assert outage["coverage"]["input_rejected"] is False
