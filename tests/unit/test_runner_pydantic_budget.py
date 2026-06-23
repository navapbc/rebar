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

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from rebar.llm.config import LLMConfig  # noqa: E402
from rebar.llm.errors import LLMRunnerError  # noqa: E402
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
