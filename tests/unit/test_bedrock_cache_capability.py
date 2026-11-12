"""Bedrock models that cannot cache prompts are not treated as having requested caching.

Bug tardy-coltish-swellfish. Every Bedrock profile was classified ``prompt_cache_style="bedrock"``
whatever its ``bedrock_supports_prompt_caching`` said, so ``cache_settings_for`` returned Bedrock
cache settings for mistral, qwen, meta and others. pydantic-ai ignores them for such a model, so
both cache counters came back 0 and the runner logged "prompt caching requested ... but had NO
effect" on every large call, although caching was never possible.

The style itself must stay "bedrock" for these models: ``structured.native_output_stack_supported``
keys on it, and qwen and others do have native structured output. The runs below drive the real
``build_bedrock_provider`` with only boto3's session replaced by a scripted Converse client.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("boto3")

from rebar.llm.capabilities import ModelCapabilities, cache_settings_for

pytestmark = pytest.mark.unit

_NON_CACHING = [
    "mistral.mistral-large-2402-v1:0",
    "qwen.qwen3-32b-v1:0",
    "us.meta.llama4-maverick-17b-instruct-v1:0",
]
_CACHING = ["us.anthropic.claude-sonnet-4-6", "us.amazon.nova-pro-v1:0"]
_MODES = ("single_turn", "agentic")
_CACHE_WARNING = "prompt caching requested"


class _ScriptedClient:
    """A ``bedrock-runtime`` stand-in answering every ``converse`` with one large, uncached turn."""

    meta = SimpleNamespace(region_name="us-west-2", endpoint_url="https://bedrock.invalid")

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 20_000, "outputTokens": 1, "totalTokens": 20_001},
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }


class _Session:
    region_name = "us-west-2"

    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    def client(self, service_name, **_kwargs):
        return self._client


def _cfg(tmp_path, model_id: str):
    from rebar.llm.config import LLMConfig

    return LLMConfig(
        repo_path=str(tmp_path),
        model=f"bedrock:{model_id}",
        bedrock_region_name="us-west-2",
        llm_retry_max_attempts=1,
    )


def _caps(tmp_path, model_id: str) -> ModelCapabilities:
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.capabilities import _capabilities_from_profile

    provider = build_bedrock_provider(_cfg(tmp_path, model_id), session=_Session(_ScriptedClient()))
    return _capabilities_from_profile(provider.model_profile(model_id), model_id)


@pytest.mark.parametrize("model_id", _NON_CACHING)
def test_a_non_caching_bedrock_model_gets_no_cache_settings(tmp_path, model_id) -> None:
    caps = _caps(tmp_path, model_id)

    assert caps.prompt_caching is False
    assert caps.prompt_cache_style == "bedrock", "structured-output routing keys on the style"
    for mode in _MODES:
        assert cache_settings_for(caps, execution_mode=mode) is None


def test_native_output_routing_still_sees_a_non_caching_model_as_bedrock(tmp_path) -> None:
    from rebar.llm import structured

    caps = _caps(tmp_path, "qwen.qwen3-32b-v1:0")

    assert caps.native_structured_output is True
    probe = structured._bedrock_converse_output_config_support
    try:
        structured._bedrock_converse_output_config_support = lambda: False
        assert structured.native_output_stack_supported(caps) is False
    finally:
        structured._bedrock_converse_output_config_support = probe


@pytest.mark.parametrize("model_id", _CACHING)
def test_a_caching_bedrock_model_keeps_its_cache_settings(tmp_path, model_id) -> None:
    caps = _caps(tmp_path, model_id)

    assert caps.prompt_caching is True
    settings = cache_settings_for(caps, execution_mode="agentic")
    assert settings is not None
    assert settings["bedrock_cache_instructions"] is True
    assert settings["bedrock_cache_messages"] is True


def _run(monkeypatch, tmp_path, model_id: str, client: _ScriptedClient):
    import boto3

    from rebar.llm.runner import PydanticAIRunner, RunRequest

    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
    monkeypatch.setattr(boto3.session, "Session", lambda **_kw: _Session(client))
    cfg = _cfg(tmp_path, model_id)
    return PydanticAIRunner(cfg).run(
        RunRequest(
            system_prompt="Review the following material carefully. " * 4_000,
            instructions="Say ok.",
            config=cfg,
            mode="text",
            execution_mode="single_turn",
        )
    )


def _cache_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and _CACHE_WARNING in r.getMessage()
    ]


def test_a_non_caching_model_logs_no_cache_warning(monkeypatch, tmp_path, caplog) -> None:
    client = _ScriptedClient()

    with caplog.at_level(logging.WARNING):
        _run(monkeypatch, tmp_path, "qwen.qwen3-32b-v1:0", client)

    assert client.calls, "converse was never called"
    assert _cache_warnings(caplog) == []
    assert "cachePoint" not in repr(client.calls[0])


def test_a_caching_model_with_zero_counters_still_warns(monkeypatch, tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        _run(monkeypatch, tmp_path, "us.anthropic.claude-sonnet-4-6", _ScriptedClient())

    assert _cache_warnings(caplog), "a caching model whose cache never engaged must still warn"


def test_the_chain_merge_takes_prompt_caching_from_the_primary() -> None:
    from rebar.llm.runner_support import _intersect_capabilities

    primary = ModelCapabilities(False, "bedrock", False, prompt_caching=False)
    fallback = ModelCapabilities(False, "bedrock", False, prompt_caching=True)

    assert _intersect_capabilities([primary, fallback]).prompt_caching is False
    assert _intersect_capabilities([fallback, primary]).prompt_caching is True
