"""Provide the RP-01 S2 happy path for one bounded structured-output operation.

An invalid prompted response followed by a valid response must use one Pydantic AI
``Agent`` operation. Aggregate usage reports two requests, which distinguishes in-run
recovery from separate ``run_sync`` calls. An offline ``FunctionModel`` prevents
provider access.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _seq_model(texts):
    """A ``FunctionModel`` returning ``texts[i]`` (clamped) on the i-th call, counting calls."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state = {"calls": 0}

    def gen(messages, info):
        i = state["calls"]
        state["calls"] += 1
        return ModelResponse(parts=[TextPart(texts[min(i, len(texts) - 1)])])

    return FunctionModel(gen), state


def _req(cfg):
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def _run(model):
    cfg = LLMConfig(repo_path=".")
    return PydanticAIRunner(cfg, model_override=model).run(_req(cfg))


def test_prompted_invalid_then_valid_is_one_bounded_operation():
    """AC#1: an invalid-then-valid prompted recovery completes in ONE Agent run reporting
    exactly two model requests under one shared budget — not two independent runs."""
    model, state = _seq_model(["not json at all", '{"verdict": "PASS"}'])
    result = _run(model)

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2, "the model was called twice (bad reply, then good)"
    assert result["_usage"]["requests"] == 2, (
        "one bounded Agent operation counts BOTH requests in the same run; the old "
        "per-attempt scheduler reported only the last run's request (== 1)"
    )
