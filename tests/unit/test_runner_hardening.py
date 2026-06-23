"""Structured-output hardening contract (post-cutover).

The pre-cutover ``_invoke_structured`` outer retry was replaced by the pydantic_ai
reliability stack's bounded retry (``structured.OUTPUT_RETRIES``). This pins the
cross-cutting CONTRACT that survives the cutover: a structured-output run recovers
from a near-miss reply within a BOUNDED number of model calls and then stops — it
never silently inflates billable calls. (The per-runner mechanics are exercised in
test_pydantic_ai_runner.py; this file pins the budget invariant via the public seam.)
"""

from __future__ import annotations

import pytest

from rebar.llm import structured as _structured
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMRunnerError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")


def _sequence_model(texts):
    """A FunctionModel that returns ``texts[i]`` on the i-th call (clamping to the
    last), and a state dict whose ``i`` counts the model calls made."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state = {"i": 0}

    def gen(messages, info):
        idx = min(state["i"], len(texts) - 1)
        state["i"] += 1
        return ModelResponse(parts=[TextPart(texts[idx])])

    return FunctionModel(gen), state


def _structured_req():
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=LLMConfig(repo_path="."),
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def test_bounded_retry_recovers_and_stops_early() -> None:
    # First reply is unparseable; the bounded retry feeds the error back and the second
    # reply validates — the run STOPS as soon as it validates (does not burn the budget).
    model, calls = _sequence_model(
        ["sorry, no JSON", '{"verdict": "FAIL", "findings": [], "summary": "no"}']
    )
    out = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(_structured_req())
    assert out["verdict"] == "FAIL"
    assert calls["i"] == 2  # recovered on the first retry
    assert calls["i"] <= 1 + _structured.OUTPUT_RETRIES  # within the bounded budget


def test_exhausting_the_budget_raises_and_does_not_inflate_calls() -> None:
    # An always-unparseable model makes EXACTLY 1 + OUTPUT_RETRIES attempts, then raises —
    # the guard against silent inflation of billable calls.
    model, calls = _sequence_model(["never any json"])
    with pytest.raises(LLMRunnerError):  # StructuredOutputError is an LLMRunnerError subclass
        PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(_structured_req())
    assert calls["i"] == 1 + _structured.OUTPUT_RETRIES


def test_text_mode_makes_a_single_call() -> None:
    # text mode needs no structured output, so it never retries.
    model, calls = _sequence_model(["just some prose"])
    req = RunRequest(
        system_prompt="x",
        instructions="y",
        config=LLMConfig(repo_path="."),
        reviewers=["v"],
        mode="text",
    )
    out = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(req)
    assert out["text"] == "just some prose"
    assert calls["i"] == 1
