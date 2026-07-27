"""The pydantic_ai runner must honour ``cfg.max_iterations`` as its model-request
budget — NOT silently fall back to pydantic-ai's default ``request_limit=50`` — and,
on exceed, raise the actionable :class:`LLMRunnerError` wrapper (mirroring the
langgraph runner's GraphRecursionError translation), not the raw
``UsageLimitExceeded``.

Offline: a ``FunctionModel`` that never terminates (always returns a ToolCallPart)
forces the budget to trip with no API call. Skips gracefully if the ``[agents]``
extra is absent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from rebar.llm.config import LLMConfig  # noqa: E402
from rebar.llm.errors import LLMRunnerError, UnretryableOutputError  # noqa: E402
from rebar.llm.runner import PydanticAIRunner, RunRequest  # noqa: E402


def test_pydantic_runner_honours_max_iterations_budget():
    calls = {"n": 0}

    def loop(messages, info: AgentInfo):
        # Never terminate: always ask to call the first tool again.
        calls["n"] += 1
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.function_tools[0].name, args={"path": "."})]
        )

    cfg = replace(LLMConfig.from_env(), runner="pydantic_ai", repo_path=".", max_iterations=5)
    runner = PydanticAIRunner(cfg, model_override=FunctionModel(loop))
    req = RunRequest(
        system_prompt="x",
        instructions="go",
        config=cfg,
        mode="text",
        reviewers=[],
    )

    with pytest.raises(LLMRunnerError):
        runner.run(req)

    # request_limit = ceil(max_iterations / 2) = ceil(5/2) = 3. The runner must stop
    # at ~3 model requests, NOT pydantic-ai's default 50.
    assert calls["n"] <= 3, f"runner ignored max_iterations: made {calls['n']} requests"


def test_bounded_evidence_removes_tools_then_returns_text():
    calls = {"n": 0}

    def gather(messages, info: AgentInfo):
        calls["n"] += 1
        if info.function_tools:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=info.function_tools[0].name,
                        args={"path": "."},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("bounded evidence summary")])

    cfg = replace(
        LLMConfig.from_env(),
        runner="pydantic_ai",
        repo_path=".",
        max_iterations=100,
    )
    request = RunRequest(
        system_prompt="x",
        instructions="gather evidence",
        config=cfg,
        mode="text",
        iteration_limit=8,
        tool_step_limit=1,
    )

    result = PydanticAIRunner(
        cfg,
        model_override=FunctionModel(gather),
    ).run(request)

    assert result["text"] == "bounded evidence summary"
    assert calls["n"] == 2


def test_bounded_evidence_truncation_keeps_failure_counters():
    def truncate(messages, info: AgentInfo):
        return ModelResponse(
            parts=[TextPart("partial evidence")],
            finish_reason="length",
        )

    cfg = replace(
        LLMConfig.from_env(),
        runner="pydantic_ai",
        repo_path=".",
    )
    request = RunRequest(
        system_prompt="x",
        instructions="gather evidence",
        config=cfg,
        mode="text",
        iteration_limit=8,
        tool_step_limit=1,
    )

    with pytest.raises(UnretryableOutputError) as caught:
        PydanticAIRunner(
            cfg,
            model_override=FunctionModel(truncate),
        ).run(request)

    diagnostic = caught.value.diagnostic
    assert diagnostic["finish_reason"] == "length"
    assert diagnostic["requests"] == 1
    assert diagnostic["request_limit"] == 4
    assert diagnostic["tool_calls"] == 0


def test_prompted_structured_retry_aggregates_terminal_failure_telemetry():
    calls = {"n": 0}

    def malformed_then_truncated(messages, info: AgentInfo):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[TextPart("not valid completion JSON")])
        return ModelResponse(
            parts=[TextPart('{"verdict":"PASS"')],
            finish_reason="length",
        )

    cfg = replace(
        LLMConfig.from_env(),
        runner="pydantic_ai",
        repo_path=".",
        model="anthropic:claude-opus-4-8",
    )
    request = RunRequest(
        system_prompt="x",
        instructions="return a completion verdict",
        config=cfg,
        mode="structured",
        output_schema="completion_verdict",
        execution_mode="single_turn",
    )

    with pytest.raises(UnretryableOutputError) as caught:
        PydanticAIRunner(
            cfg,
            model_override=FunctionModel(malformed_then_truncated),
        ).run(request)

    diagnostic = caught.value.diagnostic
    assert calls["n"] == 2
    assert diagnostic["requests"] == 2
    assert diagnostic["finish_reason"] == "length"
