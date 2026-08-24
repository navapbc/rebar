"""Migration behaviour for the OpenAI Responses-API cutover (story 155c,
``upbeat-illadvised-springtail``).

The wire-level behavioural diff between ``openai-chat:`` and ``openai-responses:`` is proven by
``tests/unit/test_openai_responses_contract.py`` (landed with the blocking research ticket). This
module pins the MIGRATION itself: the default flip of hosted OpenAI to the Responses API, the
custom-endpoint carve-out that keeps Chat Completions, and the once-per-run deprecation notice the
runner emits for the hosted ``openai-chat:`` fallback.
"""

from __future__ import annotations

import logging

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import _resolve_target

_DEPRECATION_KEY = "cfg:the hosted-OpenAI 'openai-chat:' provider prefix"


# ── the cutover: hosted OpenAI defaults to the Responses API ──────────────────────────────────
@pytest.mark.parametrize(
    ("model", "provider"),
    [("gpt-4o", None), ("openai:gpt-4o", None), ("gpt-4o", "openai"), ("openai:gpt-4o", "openai")],
)
def test_hosted_openai_defaults_to_responses(model, provider):
    assert _resolve_target(model, provider) == "openai-responses:gpt-4o"


@pytest.mark.parametrize("provider", [None, "openai"])
def test_a_custom_endpoint_keeps_chat_completions(provider):
    """A custom OpenAI-compatible endpoint cannot use the Responses API in rebar (matrix rows
    2-3), so the flip must not apply when an endpoint is configured."""
    assert (
        _resolve_target("openai:gpt-4o", provider, endpoint="http://local:1234/v1")
        == "openai-chat:gpt-4o"
    )


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("openai-chat:gpt-4o", None),
        ("openai-chat:gpt-4o", "openai-chat"),
        ("gpt-4o", "openai-chat"),
    ],
)
def test_explicit_openai_chat_is_preserved_as_the_fallback(model, provider):
    assert _resolve_target(model, provider) == "openai-chat:gpt-4o"


# ── the fallback deprecation is registered with a pre-v1 removal target ───────────────────────
def test_openai_chat_fallback_is_a_registered_scheduled_deprecation():
    from rebar._deprecations import REGISTRY

    dep = REGISTRY[_DEPRECATION_KEY]
    assert dep.kind == "cfg"
    assert dep.permanent is False
    assert dep.remove_in == "v1.0.0"
    assert "openai-responses:" in dep.replacement


# ── the runner emits the notice once for HOSTED chat, and NOT for a custom endpoint ───────────
def _offline_runner(cfg):
    """A runner whose model is a FunctionModel, so ``run`` makes no provider call but the model
    string is still resolved (and the deprecation check still runs)."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.runner import PydanticAIRunner

    def gen(messages, info):
        return ModelResponse(parts=[TextPart('{"findings": [], "summary": "ok"}')])

    return PydanticAIRunner(cfg, model_override=FunctionModel(gen))


def _run(runner):
    from rebar.llm.runner import RunRequest

    return runner.run(
        RunRequest(
            system_prompt="s",
            instructions="i",
            config=runner._config,
            reviewers=["code-quality"],
            mode="findings",
            output_schema="review_result",
        )
    )


def test_hosted_openai_chat_emits_the_deprecation_notice(caplog):
    pytest.importorskip("pydantic_ai")
    cfg = LLMConfig(model="openai-chat:gpt-4o", repo_path=".")
    with caplog.at_level(logging.WARNING, logger="rebar.llm.runner"):
        _run(_offline_runner(cfg))
    hits = [r for r in caplog.records if _DEPRECATION_KEY.split(":", 1)[1] in r.getMessage()]
    assert len(hits) == 1, f"expected exactly one deprecation notice, got {len(hits)}"
    assert "openai-responses:" in hits[0].getMessage()


def test_custom_endpoint_chat_does_not_emit_the_deprecation_notice(caplog):
    pytest.importorskip("pydantic_ai")
    # bare openai + custom base_url resolves to openai-chat by construction, not opt-in → no notice
    cfg = LLMConfig(model="openai:gpt-4o", base_url="http://localhost:1234/v1", repo_path=".")
    with caplog.at_level(logging.WARNING, logger="rebar.llm.runner"):
        _run(_offline_runner(cfg))
    hits = [r for r in caplog.records if _DEPRECATION_KEY.split(":", 1)[1] in r.getMessage()]
    assert hits == []


def test_responses_default_does_not_emit_the_deprecation_notice(caplog):
    pytest.importorskip("pydantic_ai")
    cfg = LLMConfig(model="gpt-4o", repo_path=".")
    with caplog.at_level(logging.WARNING, logger="rebar.llm.runner"):
        _run(_offline_runner(cfg))
    hits = [r for r in caplog.records if _DEPRECATION_KEY.split(":", 1)[1] in r.getMessage()]
    assert hits == []
