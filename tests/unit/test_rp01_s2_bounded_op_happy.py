"""RP-01 S2 — happy path for "run structured-output recovery as ONE bounded Agent
operation" (ticket [rebar:kingsize-unfair-blackbird], 66b3-4214-c70b-4a9a).

This is the SINGLE happy-path specification handed to the implementer. It pins the core
contract: a prompted invalid-then-valid recovery is driven as ONE bounded pydantic-ai
``Agent`` run (composing the S1 output-policy adapter), so the whole recovery reports
under one shared run rather than as a sequence of independent ``run_sync`` calls.

Observable teeth: today's manual scheduler issues a SEPARATE ``run_sync`` per attempt, so
``_extract_usage`` reports only the LAST run's ``requests`` (== 1). One bounded operation
counts BOTH model requests in the same run (``requests`` == 2). Asserting the request count
— not any private structure — is what distinguishes the new behavior from the old.

Drives the REAL ``PydanticAIRunner`` over an offline ``FunctionModel``; no live/billable
call can escape (ALLOW_MODEL_REQUESTS is forced off).
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
