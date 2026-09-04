"""Migration behaviour for the OpenAI Responses-API cutover (story 155c,
``upbeat-illadvised-springtail``).

The wire-level behavioural diff between ``openai-chat:`` and ``openai-responses:`` is proven by
``tests/unit/test_openai_responses_contract.py`` (landed with the blocking research ticket). This
module pins the MIGRATION itself: the default flip of hosted OpenAI to the Responses API, the
custom-endpoint carve-out that keeps Chat Completions, and the pre-1.0 removal of the hosted
``openai-chat:`` opt-in fallback.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import _resolve_target


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
def test_hosted_explicit_openai_chat_resolves_to_responses(model, provider):
    assert _resolve_target(model, provider) == "openai-responses:gpt-4o"


def test_custom_endpoint_explicit_openai_chat_keeps_chat_completions():
    endpoint = "http://local:1234/v1"
    assert _resolve_target("openai-chat:gpt-4o", None, endpoint=endpoint) == "openai-chat:gpt-4o"
    assert _resolve_target("gpt-4o", "openai-chat", endpoint=endpoint) == "openai-chat:gpt-4o"


# ── the fallback deprecation was removed with the hosted fallback itself ─────────────────────
def test_openai_chat_fallback_is_no_longer_a_registered_scheduled_deprecation():
    from rebar._deprecations import REGISTRY

    assert "cfg:the hosted-OpenAI 'openai-chat:' provider prefix" not in REGISTRY


# ── runner/provider integration keeps custom endpoint Chat and hosted Responses ──────────────
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


def test_hosted_openai_chat_uses_responses_without_a_deprecation_notice(caplog):
    pytest.importorskip("pydantic_ai")
    cfg = LLMConfig(model="openai-chat:gpt-4o", repo_path=".")
    runner = _offline_runner(cfg)
    assert runner._config.model == "openai-chat:gpt-4o"
    _run(runner)
    assert "hosted-OpenAI" not in caplog.text


def test_responses_default_does_not_emit_the_deprecation_notice(caplog):
    pytest.importorskip("pydantic_ai")
    cfg = LLMConfig(model="gpt-4o", repo_path=".")
    _run(_offline_runner(cfg))
    assert "hosted-OpenAI" not in caplog.text


def test_a_slot_endpoint_is_recovered_and_keeps_chat(caplog, monkeypatch):
    """A model-class SLOT ``endpoint`` (no top-level ``base_url``) forces Chat by construction:
    the runner must fold it into ``cfg.base_url`` AND stay silent — the hosted-chat deprecation is
    for an opt-in ``openai-chat:``, not for an endpoint-forced one (ticket 155c). Drives the
    ``_recover_slot_endpoint`` path the top-level ``base_url`` cases cannot reach (that recovery is
    skipped when a ``model_override`` is set)."""
    pytest.importorskip("pydantic_ai")
    import rebar.llm.runner as runner_mod
    from rebar.llm.runner import PydanticAIRunner

    monkeypatch.setattr(
        runner_mod, "primary_endpoint_for", lambda resolved, repo_root=None: "http://slot:9/v1"
    )
    runner = PydanticAIRunner(LLMConfig(model="openai-chat:gpt-4o", repo_path="."))
    recovered = runner._recover_slot_endpoint(runner._config, "openai-chat:gpt-4o")
    assert recovered.base_url == "http://slot:9/v1"
    assert "hosted-OpenAI" not in caplog.text
