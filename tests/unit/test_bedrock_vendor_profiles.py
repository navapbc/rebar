"""Every Bedrock vendor gets a Bedrock profile, so agentic runs work on Llama models.

Bug garlicky-leery-seal. pydantic-ai 1.x's ``BedrockProvider.model_profile`` returns a plain
``ModelProfile`` for ``meta`` models (``None`` for an unknown vendor), yet
``BedrockConverseModel`` reads ``bedrock_supports_strict_tool_definition`` from the profile
directly whenever a tool is strict. Every agentic run on a ``us.meta.*`` model therefore raised
``LLMUnavailableError: ... bedrock_supports_strict_tool_definition`` before any request was sent.

These tests drive the real ``build_bedrock_provider`` (only boto3's session is replaced, by a
scripted ``bedrock-runtime`` client), so the fix is exercised rather than stubbed out.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("boto3")

pytestmark = pytest.mark.unit

_MAVERICK = "us.meta.llama4-maverick-17b-instruct-v1:0"

# Every vendor prefix pydantic-ai 1.107's BedrockProvider.model_profile knows, plus an unknown one.
_VENDOR_MODEL_IDS = [
    "us.anthropic.claude-sonnet-4-6",
    "mistral.mistral-large-2402-v1:0",
    "cohere.command-r-v1:0",
    "amazon.nova-pro-v1:0",
    _MAVERICK,
    "us.deepseek.r1-v1:0",
    "openai.gpt-oss-120b-1:0",
    "qwen.qwen3-32b-v1:0",
    "google.gemma-3-27b-it",
    "minimax.minimax-m2",
    "nvidia.nemotron-nano-9b-v2",
    "unknownvendor.some-model-v1:0",
]


def _response(content: list[dict], stop_reason: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


_DONE = _response([{"text": "done"}], "end_turn")


class _ScriptedClient:
    """A ``bedrock-runtime`` stand-in that records ``converse`` calls and replays a script."""

    meta = SimpleNamespace(region_name="us-west-2", endpoint_url="https://bedrock.invalid")

    def __init__(self, *script: dict):
        self.script = list(script) or [_DONE]
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.script[min(len(self.calls), len(self.script)) - 1]


class _Session:
    region_name = "us-west-2"

    def __init__(self, client: _ScriptedClient):
        self._client = client

    def client(self, service_name, **_kwargs):
        assert service_name == "bedrock-runtime"
        return self._client


def _cfg(tmp_path, model_id: str):
    from rebar.llm.config import LLMConfig

    return LLMConfig(
        repo_path=str(tmp_path),
        model=f"bedrock:{model_id}",
        bedrock_region_name="us-west-2",
        llm_retry_max_attempts=1,
    )


def _run_agentic(
    monkeypatch,
    tmp_path,
    client: _ScriptedClient,
    model_id: str = _MAVERICK,
    execution_mode: str = "agentic",
):
    import boto3

    from rebar.llm.runner import PydanticAIRunner, RunRequest

    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
    monkeypatch.setattr(boto3.session, "Session", lambda **_kw: _Session(client))
    cfg = _cfg(tmp_path, model_id)
    return PydanticAIRunner(cfg).run(
        RunRequest(
            system_prompt="sys",
            instructions="Read notes.txt, then answer.",
            config=cfg,
            mode="text",
            execution_mode=execution_mode,
        )
    )


def _blocks(call: dict) -> list[dict]:
    """Every content/system/tool block a Converse call carried."""
    blocks = list(call.get("system") or [])
    for message in call.get("messages") or []:
        blocks.extend(message.get("content") or [])
    blocks.extend((call.get("toolConfig") or {}).get("tools") or [])
    return blocks


def test_agentic_run_on_a_meta_model_completes(monkeypatch, tmp_path):
    client = _ScriptedClient()
    result = _run_agentic(monkeypatch, tmp_path, client)
    assert result["text"] == "done"
    assert client.calls[0]["modelId"] == _MAVERICK
    assert client.calls[0]["toolConfig"]["tools"], "the agentic run sent no tools"


def test_agentic_run_on_a_meta_model_completes_a_tool_turn(monkeypatch, tmp_path):
    (tmp_path / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")
    tool_use = {"toolUse": {"toolUseId": "t1", "name": "read_file", "input": {"path": "notes.txt"}}}
    client = _ScriptedClient(_response([tool_use], "tool_use"), _DONE)

    result = _run_agentic(monkeypatch, tmp_path, client)

    assert result["text"] == "done"
    assert len(client.calls) == 2
    results = [b["toolResult"] for b in _blocks(client.calls[1]) if "toolResult" in b]
    assert [r["toolUseId"] for r in results] == ["t1"]
    assert "the answer is 42" in str(results[0]["content"])
    # meta has no Bedrock prompt caching: no cache point may reach the request.
    for call in client.calls:
        assert not [b for b in _blocks(call) if "cachePoint" in b], call


@pytest.mark.parametrize("execution_mode", ["single_turn", "agentic"])
def test_a_run_with_no_fallback_chain_attests_the_model_that_ran(
    monkeypatch, tmp_path, execution_mode
):
    """``provider_provenance.ran_model`` names the model that served the call even when the
    config has no fallback chain (bug botchy-felt-bluefish): the engine-comparison harness and
    the replay corpus read it, and a single-model run used to leave it unset."""
    client = _ScriptedClient()

    result = _run_agentic(monkeypatch, tmp_path, client, execution_mode=execution_mode)

    assert client.calls[0]["modelId"] == _MAVERICK
    assert result["provider_provenance"]["ran_model"] == f"bedrock:{_MAVERICK}"
    assert result["provider_provenance"]["ran_model"] == result["model"]


@pytest.mark.parametrize("model_id", [_MAVERICK, "unknownvendor.some-model-v1:0"])
def test_formerly_plain_profiles_classify_like_other_non_caching_bedrock_vendors(
    tmp_path, model_id
):
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.capabilities import _capabilities_from_profile

    provider = build_bedrock_provider(_cfg(tmp_path, model_id), session=_Session(_ScriptedClient()))
    profile = provider.model_profile(model_id)
    caps = _capabilities_from_profile(profile, model_id)

    assert caps.prompt_cache_style == "bedrock"
    assert caps.native_structured_output is False
    assert profile.bedrock_supports_prompt_caching is False
    assert profile.bedrock_supports_tool_caching is False
    assert profile.bedrock_supports_strict_tool_definition is False


@pytest.mark.parametrize("model_id", _VENDOR_MODEL_IDS)
def test_every_vendor_resolves_to_a_bedrock_profile_and_existing_ones_are_unchanged(
    tmp_path, model_id
):
    from pydantic_ai.providers.bedrock import BedrockModelProfile, BedrockProvider

    from rebar.llm.bedrock_model import build_bedrock_provider

    client = _ScriptedClient()
    provider = build_bedrock_provider(_cfg(tmp_path, model_id), session=_Session(client))
    upstream = BedrockProvider(bedrock_client=client).model_profile(model_id)

    profile = provider.model_profile(model_id)

    assert isinstance(profile, BedrockModelProfile)
    if isinstance(upstream, BedrockModelProfile):
        assert profile == upstream
