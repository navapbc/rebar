"""Bug dd27: the AGENTIC message tail was re-sent UNCACHED every turn.

``cache_settings_for`` set exactly two of the four available breakpoints (instructions +
tool definitions), so on the agentic path the GROWING tool-result history rode outside every
breakpoint and was re-sent uncached on each turn — input cost O(N^2) in turns.

It was invisible to rebar's own telemetry: ``warn_if_cache_ineffective`` requires
``cache_read_tokens == 0``, but the SYSTEM block WAS hitting cache, so the counter was never
zero and the predicate could never fire. A usage log would never have surfaced it.

THE DISCRIMINATING EVIDENCE IS NOT "A BREAKPOINT EXISTS" — two breakpoints existed before this
bug was filed, which is precisely why a settings-shaped assertion cannot tell the fixed code
from the broken code. The evidence is the CAPTURED OUTBOUND REQUEST:

* agentic/anthropic — the request carries the automatic-caching directive that puts a
  breakpoint AFTER the accumulated history, and the request grows a breakpoint it did not
  have before;
* agentic/bedrock — the same intent expressed with BEDROCK'S OWN key, landing as a
  ``cachePoint`` on the last message, and NEVER as a top-level ``cache_control``. This is the
  trap: Bedrock has no ``bedrock_cache`` automatic key and rejects a top-level
  ``cache_control`` outright ("Extra inputs are not permitted"). LangChain shipped exactly
  this regression — their prompt-caching middleware broke on Bedrock in 1.4.1 by switching to
  top-level automatic caching (langchain#37042);
* single_turn — BYTE-IDENTICAL to the pre-fix request. That is the regression this change
  could plausibly cause, so it is pinned against a FROZEN copy of the pre-fix settings
  (:data:`_PRE_FIX_ANTHROPIC_SETTINGS` / :data:`_PRE_FIX_BEDROCK_SETTINGS`) rather than
  against the implementation's own output, which would make the assertion a tautology.

Capture technique is story 0d76's, reused rather than reinvented: drive the PRODUCTION path
(the real ``_build_retrying_anthropic_model`` builder behind its ``_wrapped_transport`` seam;
the real ``BedrockConverseModel`` over an intercepted ``converse``) until the request is fully
assembled, then abort at the wire boundary. Zero network calls, zero tokens.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from rebar.llm import anthropic_model as anthropic_model_mod
from rebar.llm import structured_run as structured_run_mod

pytest.importorskip("pydantic_ai")

import httpx
import pydantic_ai.models
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from rebar.llm.capabilities import ModelCapabilities, cache_settings_for

pytestmark = pytest.mark.unit


# The EXACT settings the pre-fix `cache_settings_for` produced, frozen here so the
# single-turn no-change claim is checked against a constant rather than against whatever the
# implementation currently emits.
_PRE_FIX_ANTHROPIC_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,
    "anthropic_cache_tool_definitions": True,
}
_PRE_FIX_BEDROCK_SETTINGS: dict[str, Any] = {
    "bedrock_cache_instructions": True,
    "bedrock_cache_tool_definitions": True,
}

_MODEL = "claude-sonnet-4-6"
_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6-20250101-v1:0"


def _anthropic_expects_httpx2_client() -> bool:
    import inspect

    import anthropic

    http_client = inspect.signature(anthropic.AsyncAnthropic.__init__).parameters.get("http_client")
    return http_client is not None and "httpx2.AsyncClient" in str(http_client.annotation)


def _transport_http_module():
    if _anthropic_expects_httpx2_client():
        return pytest.importorskip("httpx2")
    return httpx


def _caps(style: str) -> ModelCapabilities:
    return ModelCapabilities(
        native_structured_output=False,
        prompt_cache_style=style,
        supports_thinking=True,
        supports_temperature=True,
    )


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read_file",
            description="read a file",
            parameters_json_schema={"type": "object", "properties": {"p": {"type": "string"}}},
        )
    ]


def _multi_turn_messages(turns: int = 4) -> list[Any]:
    """A tool loop mid-flight: system + user, then `turns` rounds of tool-call/tool-result.

    This is the shape the bug is about — the tool-result blocks are the accumulated history
    that was re-sent uncached on every subsequent turn."""
    messages: list[Any] = [
        ModelRequest(parts=[SystemPromptPart(content="SYS"), UserPromptPart(content="go")])
    ]
    for i in range(turns):
        messages.append(
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="read_file", args={"p": f"f{i}"}, tool_call_id=f"c{i}")
                ]
            )
        )
        messages.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="X" * 400, tool_call_id=f"c{i}")
                ]
            )
        )
    return messages


def _single_turn_messages() -> list[Any]:
    """One structured call, no tools, no history — rebar's single_turn shape."""
    return [ModelRequest(parts=[SystemPromptPart(content="SYS"), UserPromptPart(content="go")])]


# ── capture harnesses (story 0d76's technique) ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """The real `AsyncAnthropic` builds auth headers before the transport is reached, so it
    needs *a* key present; it is never sent anywhere. `ANTHROPIC_BASE_URL` is cleared so a dev
    machine's ambient loopback proxy cannot steer the builder down its bypass branch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)


def _capture_anthropic_body(
    settings: Any, messages: list[Any], tools: list[ToolDefinition]
) -> bytes:
    """The raw outbound request BODY the production Anthropic path would put on the wire.

    Everything except the socket is real: `_build_retrying_anthropic_model` is the production
    builder, and the MockTransport is installed through its own `_wrapped_transport` test seam,
    so the real `AsyncTenacityTransport`/`AsyncAnthropic`/`AnthropicModel` all participate."""
    from rebar.llm.anthropic_model import _build_retrying_anthropic_model
    from rebar.llm.config import LLMConfig

    captured: list[bytes] = []

    transport_http = _transport_http_module()

    def _handler(request):
        captured.append(request.content)
        return transport_http.Response(
            200,
            json={
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "model": _MODEL,
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    cfg = LLMConfig(repo_path=".", model=f"anthropic:{_MODEL}")
    model, client = _build_retrying_anthropic_model(
        _MODEL, base_url=None, cfg=cfg, _wrapped_transport=transport_http.MockTransport(_handler)
    )
    try:
        asyncio.run(model.request(messages, settings, ModelRequestParameters(function_tools=tools)))
    finally:
        asyncio.run(client.aclose())
    assert len(captured) == 1, "expected exactly one assembled request"
    return captured[0]


class _WireBoundary(Exception):
    """Raised from the intercepted `converse` once the request is fully assembled."""


def _capture_bedrock_request(
    settings: Any, messages: list[Any], tools: list[ToolDefinition]
) -> dict:
    """The kwargs the production Bedrock path would hand boto3's `converse`.

    `converse` aborts at the wire boundary the moment the request is assembled, so the
    request-building half of the production path runs in full with no client, credentials,
    network or tokens involved."""
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    captured: dict = {}

    class _InterceptingClient:
        meta = SimpleNamespace(region_name="us-east-1", endpoint_url="https://bedrock.invalid")

        def converse(self, **kwargs):
            captured.update(kwargs)
            raise _WireBoundary

    model = BedrockConverseModel(
        _BEDROCK_MODEL, provider=BedrockProvider(bedrock_client=_InterceptingClient())
    )
    with pytest.raises(_WireBoundary):
        asyncio.run(model.request(messages, settings, ModelRequestParameters(function_tools=tools)))
    assert captured, "the request was never assembled"
    return captured


# ── §A the settings seam: the two arms must use their OWN keys ───────────────────────────


def test_agentic_anthropic_adds_the_automatic_message_breakpoint() -> None:
    """THE defect, at the settings seam. The agentic arm keeps both existing breakpoints and
    adds `anthropic_cache` — the automatic key that puts a breakpoint after the accumulated
    history. `anthropic_cache_messages` must NOT also be set: pydantic-ai raises
    `UserError('anthropic_cache and anthropic_cache_messages cannot both be enabled')`."""
    settings = cache_settings_for(_caps("anthropic"), execution_mode="agentic")
    assert settings is not None
    assert settings["anthropic_cache"] is True
    assert settings["anthropic_cache_instructions"] is True
    assert settings["anthropic_cache_tool_definitions"] is True
    assert "anthropic_cache_messages" not in settings, (
        "mutually exclusive with anthropic_cache — pydantic-ai raises UserError"
    )


def test_single_turn_anthropic_settings_are_exactly_the_pre_fix_settings() -> None:
    """The no-regression half at the settings seam, pinned against the FROZEN pre-fix dict."""
    settings = cache_settings_for(_caps("anthropic"), execution_mode="single_turn")
    assert dict(settings) == _PRE_FIX_ANTHROPIC_SETTINGS


def test_agentic_bedrock_uses_bedrocks_own_message_key_not_anthropics() -> None:
    """THE TRAP. Bedrock has NO `bedrock_cache` automatic key and REJECTS a top-level
    `cache_control`, so the two arms cannot share one setting. LangChain shipped exactly this
    regression (langchain#37042)."""
    pytest.importorskip("boto3")
    settings = cache_settings_for(_caps("bedrock"), execution_mode="agentic")
    assert settings is not None
    assert settings["bedrock_cache_messages"] is True
    assert settings["bedrock_cache_instructions"] is True
    assert settings["bedrock_cache_tool_definitions"] is True
    for anthropic_shaped in (
        "anthropic_cache",
        "anthropic_cache_messages",
        "anthropic_cache_instructions",
        "anthropic_cache_tool_definitions",
        "cache_control",
    ):
        assert anthropic_shaped not in settings, (
            f"{anthropic_shaped} is not a Bedrock key — it would be rejected on the wire"
        )
    assert "bedrock_cache" not in settings, "no such key exists in BedrockModelSettings"


def test_single_turn_bedrock_settings_are_exactly_the_pre_fix_settings() -> None:
    pytest.importorskip("boto3")
    settings = cache_settings_for(_caps("bedrock"), execution_mode="single_turn")
    assert dict(settings) == _PRE_FIX_BEDROCK_SETTINGS


def test_the_two_arms_share_no_message_cache_key() -> None:
    """Provider asymmetry, stated as an invariant rather than as two separate expectations:
    the message-tail key each arm uses must be absent from the other."""
    pytest.importorskip("boto3")
    anthropic = cache_settings_for(_caps("anthropic"), execution_mode="agentic")
    bedrock = cache_settings_for(_caps("bedrock"), execution_mode="agentic")
    assert set(anthropic) & set(bedrock) == set(), (
        "the arms must not share ANY key — each provider rejects the other's"
    )


def test_a_non_caching_provider_gets_no_settings_in_either_mode() -> None:
    for mode in ("agentic", "single_turn"):
        assert cache_settings_for(_caps("none"), execution_mode=mode) is None


def test_an_unknown_execution_mode_is_treated_as_single_turn() -> None:
    """Fail SAFE, not open. An unrecognised mode must not silently opt into a breakpoint whose
    budget/cost profile was never assessed for it; it degrades to today's behaviour."""
    settings = cache_settings_for(_caps("anthropic"), execution_mode="some_future_mode")
    assert dict(settings) == _PRE_FIX_ANTHROPIC_SETTINGS


# ── §B the captured request: what actually goes on the wire ──────────────────────────────


def test_captured_agentic_request_carries_a_breakpoint_after_the_message_history() -> None:
    """THE discriminating assertion. Captured from the production Anthropic path with a
    mid-flight tool loop: the request carries the top-level `cache_control` directive, which is
    how Anthropic's automatic caching places a breakpoint at the END of the prompt — i.e. after
    the accumulated tool-result history — on every turn.

    The pre-fix settings are captured in the SAME run for a real before/after diff, so this
    cannot pass by the breakpoints that already existed."""
    messages, tools = _multi_turn_messages(), _tools()
    before = json.loads(_capture_anthropic_body(_PRE_FIX_ANTHROPIC_SETTINGS, messages, tools))
    after = json.loads(
        _capture_anthropic_body(
            cache_settings_for(_caps("anthropic"), execution_mode="agentic"), messages, tools
        )
    )

    assert "cache_control" not in before, "sanity: the pre-fix request had no tail breakpoint"
    assert after["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    # The accumulated history really is in the request being cached, and really is the bulk of
    # it — otherwise the O(N^2) claim would not be about these bytes.
    assert len(after["messages"]) >= 9
    assert json.dumps(after["messages"]).count("tool_result") >= 4

    # ...and NOTHING ELSE about the request changed. The tail breakpoint is the whole diff.
    assert {k: v for k, v in after.items() if k != "cache_control"} == {
        k: v for k, v in before.items() if k != "cache_control"
    }


def test_captured_single_turn_request_is_byte_identical_to_the_pre_fix_request() -> None:
    """THE regression pin, at the strongest available resolution: raw request BYTES, from the
    production builder, pre-fix settings vs. post-fix single_turn settings."""
    messages = _single_turn_messages()
    before = _capture_anthropic_body(_PRE_FIX_ANTHROPIC_SETTINGS, messages, [])
    after = _capture_anthropic_body(
        cache_settings_for(_caps("anthropic"), execution_mode="single_turn"), messages, []
    )
    assert after == before, "single_turn must be byte-identical to the pre-fix request"
    assert b"cache_control" in before, (
        "sanity: the system breakpoint IS present, so equality is not passing by both being bare"
    )
    assert json.loads(after).get("cache_control") is None, (
        "no top-level automatic-caching directive on a call with no history to cache"
    )


def test_captured_bedrock_agentic_request_caches_the_tail_without_any_cache_control() -> None:
    """The Bedrock half of the trap, on the assembled request rather than on the settings dict.

    A `cachePoint` lands on the last (tool-result) message, and the string `cache_control`
    appears NOWHERE in the request — Bedrock rejects it with "Extra inputs are not permitted"."""
    pytest.importorskip("boto3")
    messages, tools = _multi_turn_messages(), _tools()
    before = _capture_bedrock_request(_PRE_FIX_BEDROCK_SETTINGS, messages, tools)
    after = _capture_bedrock_request(
        cache_settings_for(_caps("bedrock"), execution_mode="agentic"), messages, tools
    )

    def _cache_points(msg: dict) -> int:
        return sum(1 for block in msg.get("content", []) if "cachePoint" in block)

    assert _cache_points(before["messages"][-1]) == 0, "sanity: no tail breakpoint before"
    assert _cache_points(after["messages"][-1]) == 1, "the accumulated tail is now cached"

    blob = json.dumps(after, default=str)
    assert "cache_control" not in blob, (
        "Bedrock rejects a top-level cache_control — this is langchain#37042's regression"
    )
    # Budget: system + tool definitions + message tail = 3, inside Bedrock's limit of 4.
    assert blob.count("cachePoint") == 3


def test_captured_bedrock_single_turn_request_is_identical_to_the_pre_fix_request() -> None:
    pytest.importorskip("boto3")
    messages = _single_turn_messages()
    before = _capture_bedrock_request(_PRE_FIX_BEDROCK_SETTINGS, messages, [])
    after = _capture_bedrock_request(
        cache_settings_for(_caps("bedrock"), execution_mode="single_turn"), messages, []
    )
    assert json.dumps(after, default=str) == json.dumps(before, default=str)


def test_the_anthropic_breakpoint_budget_is_not_exceeded_on_the_agentic_path() -> None:
    """pydantic-ai's `_limit_cache_points` drops the budget to `MAX_CACHE_POINTS = 3` when
    automatic caching is on, and RAISES `UserError` if system + tools alone exceed it. This
    change puts rebar at exactly 3 (system + tools + the automatic point), so the assembly must
    complete rather than raise — proven by the request existing at all, plus the explicit count
    of EXPLICIT breakpoints, which must stay at 2."""
    body = json.loads(
        _capture_anthropic_body(
            cache_settings_for(_caps("anthropic"), execution_mode="agentic"),
            _multi_turn_messages(),
            _tools(),
        )
    )
    explicit = sum(1 for block in body["system"] if "cache_control" in block)
    explicit += sum(1 for tool in body["tools"] if "cache_control" in tool)
    explicit += sum(
        1
        for message in body["messages"]
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    )
    assert explicit == 2, "instructions + tool definitions; the third point is server-applied"


# ── §C the runner threads the execution mode through ─────────────────────────────────────


def test_runner_passes_the_requests_execution_mode_to_the_cache_seam(monkeypatch) -> None:
    """Wiring: the settings fix is inert unless `run()` actually tells the seam which arm it is
    on. Asserted on the value that crossed the seam, for BOTH modes."""
    import rebar.llm.runner as runner_mod

    seen: list[str] = []

    def _spy(caps, *, execution_mode):
        seen.append(execution_mode)
        return None

    monkeypatch.setattr(runner_mod, "cache_settings_for", _spy)
    monkeypatch.setattr(
        structured_run_mod, "_pai_structured", lambda *a, **kw: ({"verdict": "PASS"}, {})
    )
    monkeypatch.setattr(
        structured_run_mod, "_import_pydantic_ai", lambda: lambda *a, **kw: object()
    )
    monkeypatch.setattr(anthropic_model_mod, "_pai_model", lambda cfg: "anthropic:fake")
    monkeypatch.setattr(anthropic_model_mod, "_local_proxy_bypass_base_url", lambda: None)
    monkeypatch.setattr(
        runner_mod._findings,
        "finalize_outcome",
        lambda outcome, **kw: outcome["structured_response"],
    )

    class _NoBuildProviderSession:
        def __init__(self, _cfg):
            pass

        def supports(self, _name):
            return False

        def is_resolvable(self, _name):
            return True

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(runner_mod, "ProviderSession", _NoBuildProviderSession)
    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    cfg = LLMConfig(repo_path=".", model="anthropic:claude-sonnet-4-6")
    for mode in ("single_turn", "agentic"):
        PydanticAIRunner(cfg).run(
            RunRequest(
                system_prompt="sys",
                instructions="ins",
                config=cfg,
                execution_mode=mode,
                mode="structured",
                output_schema="completion_verdict",
            )
        )
    assert seen == ["single_turn", "agentic"]
