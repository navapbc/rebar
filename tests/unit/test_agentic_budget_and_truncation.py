"""Regression tests for the plan-review step-budget exhaustion + truncation retry-storm
(joe-debug RC-A + RC-B).

RC-A: the framework agent step budget was 50 (~25 tool-call cycles) — far too low for an
agentic review, so a code-grounding finder / multi-child container call exhausted it and
raised a step-budget ``LLMRunnerError``. The default is now 250.

RC-B: a TRUNCATED structured-output turn (the model hit its output-token cap) is a
complete, unusable response that re-running the SAME call reliably reproduces — yet the
bounded retry re-paid the full (agentic) call ``1 + OUTPUT_RETRIES = 3`` times before
failing. Two fixes: (1) ``cfg.max_tokens`` is now actually WIRED into the model call
(previously dropped → pydantic-ai's 4096 default applied, guaranteeing truncation on a
multi-child review); (2) truncation/refusal/content-filter raise ``UnretryableOutputError``
so the retry loop FAST-FAILS instead of retrying a deterministically-doomed call.
"""

from __future__ import annotations

import pytest

from rebar.llm import structured
from rebar.llm.config import DEFAULT_MAX_ITERATIONS, DEFAULT_MAX_TOKENS, LLMConfig
from rebar.llm.errors import StructuredOutputError, UnretryableOutputError
from rebar.llm.runner import effective_max_iterations


# ── RC-A: the framework step budget is review-appropriate by default ──────────────────
def test_default_step_budget_raised_to_review_appropriate() -> None:
    assert DEFAULT_MAX_ITERATIONS == 250
    # A default config (no per-op floor, no operator override) now grants ~125 tool-call
    # cycles — at 50 it was ~25, which exhausted on an agentic review.
    assert effective_max_iterations(LLMConfig().max_iterations, None) == 250


# ── RC-B (2): truncation / refusal / filter are UNRETRYABLE; transient error is not ───
@pytest.mark.parametrize("reason", ["max_tokens", "length", "refusal", "content_filter"])
def test_truncation_and_refusal_are_unretryable(reason: str) -> None:
    # The model hit the output cap (truncation) or refused/was filtered — re-running the
    # SAME call reproduces it, so it must be the distinct, non-retried error.
    with pytest.raises(UnretryableOutputError):
        structured.check_stop_reason(reason)


def test_transient_error_stays_retryable() -> None:
    # A transient run/provider ``error`` is a plain (retryable) StructuredOutputError —
    # NOT the unretryable subclass — because a re-run may succeed.
    with pytest.raises(StructuredOutputError) as exc:
        structured.check_stop_reason("error")
    assert not isinstance(exc.value, UnretryableOutputError)


@pytest.mark.parametrize("reason", ["end_turn", "stop", "tool_call", None])
def test_normal_stop_reasons_pass(reason: str | None) -> None:
    structured.check_stop_reason(reason)  # no raise


def test_unretryable_is_a_structured_output_error() -> None:
    # Subclassing keeps every existing ``except StructuredOutputError`` / ``except LLMError``
    # handler (the runner pass-through, the container fan-out fail-open) catching it.
    assert issubclass(UnretryableOutputError, StructuredOutputError)


# ── RC-B (1): the configured output cap is non-trivial (it's now wired to the model) ──
def test_output_cap_default_is_non_trivial() -> None:
    # The wired value must comfortably exceed pydantic-ai's 4096 fallback that caused the
    # truncation; 16000 is the non-streaming-safe ceiling.
    assert DEFAULT_MAX_TOKENS >= 16000
    assert LLMConfig().max_tokens >= 16000
