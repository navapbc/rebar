"""Contract tests for the OpenAI Chat-Completions vs Responses API migration (story b612-1edb).

The migration ticket (`upbeat-illadvised-springtail`, 155c-218b) needs a behavioural diff between
`openai-chat:` and `openai-responses:` before any provider is flipped. These tests ARE that
instrument: the identical assertions run against BOTH prefixes (the parametrize ids are the diff),
driving REAL pydantic-ai model construction — `infer_model` performs its production prefix→class
dispatch and genuinely builds `OpenAIChatModel` / `OpenAIResponsesModel` — against a stub server
that speaks both wire protocols. Only the socket is faked.

Injection mechanism (deliberate, and different from test_openai_compatible_provider.py): the
`monkeypatch.setattr(httpx, "AsyncHTTPTransport", ...)` seam used there only works because rebar's
`_build_openai` constructs that transport explicitly; stock pydantic-ai construction builds a bare
`AsyncClient` and never reads the module attribute. So each test builds ONE explicit production
`OpenAIProvider(base_url=…, api_key=…, http_client=AsyncClient(transport=MockTransport(…)))` and
hands it to `infer_model(model_string, provider_factory=…)` — the same `provider_factory` hook
rebar's own `ProviderSession.model_for` uses in production. Nothing is monkeypatched.

Scope note (plan-reviewed): the 429 case pins the DEFAULT retry surface of the stack the tests
construct (no pydantic-ai model-layer retry; the openai SDK's own max_retries=2 underneath;
`ModelHTTPError` with the status) across both APIs. It is NOT a claim about
`ProviderSession._build_openai`'s transport — that builder cannot construct `openai-responses:` at
all today, which is recorded as a constraint on the ticket, not tested here.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("openai")

import pydantic_ai.models
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import infer_model

pytestmark = pytest.mark.unit

BASE_URL = "http://stub.localdomain:1234/v1"
APIS = ("chat", "responses")
# The API-specific request path each model class must hit under a custom base_url.
EXPECTED_PATH = {"chat": "/v1/chat/completions", "responses": "/v1/responses"}


class Verdict(BaseModel):
    verdict: str
    summary: str


_VERDICT_JSON = json.dumps({"verdict": "PASS", "summary": "stub answered"})


# ── wire-shape builders: the two protocols the stub speaks ──────────────────────────────


def _chat_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def _chat_completion(content: str | None = None, tool_call: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_call is not None:
        message["tool_calls"] = [tool_call]
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_call else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _response_envelope(output: list[dict]) -> dict:
    return {
        "id": "resp_x",
        "object": "response",
        "created_at": 0,
        "model": "gpt-4o",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _responses_text(content: str) -> dict:
    return _response_envelope(
        [
            {
                "type": "message",
                "id": "msg_x",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        ]
    )


def _responses_tool_call(name: str) -> dict:
    return _response_envelope(
        [
            {
                "type": "function_call",
                "id": "fc_x",
                "call_id": "call_x",
                "name": name,
                "arguments": "{}",
                "status": "completed",
            }
        ]
    )


def _chat_stream_sse(content: str) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-x",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}}],
        }
        for piece in (content[: len(content) // 2], content[len(content) // 2 :])
    ]
    done = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    lines = [f"data: {json.dumps(c)}\n\n" for c in [*chunks, done]]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _responses_stream_sse(content: str) -> bytes:
    """The Responses streaming protocol: typed `response.*` events, not chunk objects."""
    envelope = _responses_text(content)
    events = [
        {"type": "response.created", "response": {**envelope, "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg_x",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_x",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_x",
            "output_index": 0,
            "content_index": 0,
            "delta": content,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": envelope["output"][0],
        },
        {"type": "response.completed", "response": envelope},
    ]
    out = []
    for i, event in enumerate(events):
        out.append(
            f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': i})}\n\n"
        )
    return "".join(out).encode()


# ── the dual-protocol stub, reached through real construction ───────────────────────────


class _StubServer:
    """One stub server speaking both protocols; records every request for assertions."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.tool_turns = 0
        self.status_code: int | None = None  # force an error status when set

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status_code is not None:
            return httpx.Response(
                self.status_code,
                json={"error": {"message": "slow down", "type": "rate_limit_exceeded"}},
            )
        body = _chat_body(request)
        path = request.url.path
        if path.endswith("/chat/completions"):
            return self._chat(body)
        if path.endswith("/responses"):
            return self._responses(body)
        return httpx.Response(404, json={"error": {"message": f"unexpected path {path}"}})

    def _wants_tool_turn(self, body: dict) -> bool:
        if not body.get("tools"):
            return False
        self.tool_turns += 1
        return self.tool_turns == 1

    def _chat(self, body: dict) -> httpx.Response:
        if self._wants_tool_turn(body):
            tool = body["tools"][0]["function"]["name"]
            call = {
                "id": "call_x",
                "type": "function",
                "function": {"name": tool, "arguments": "{}"},
            }
            return httpx.Response(200, json=_chat_completion(tool_call=call))
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_chat_stream_sse(_VERDICT_JSON),
            )
        return httpx.Response(200, json=_chat_completion(_VERDICT_JSON))

    def _responses(self, body: dict) -> httpx.Response:
        if self._wants_tool_turn(body):
            return httpx.Response(200, json=_responses_tool_call(body["tools"][0]["name"]))
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_responses_stream_sse(_VERDICT_JSON),
            )
        return httpx.Response(200, json=_responses_text(_VERDICT_JSON))


@pytest.fixture
def stub() -> _StubServer:
    return _StubServer()


@pytest.fixture(autouse=True)
def _allow_model_requests(monkeypatch):
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)


def _real_model(api: str, stub: _StubServer):
    """REAL construction: one explicit production OpenAIProvider carrying the stub transport,
    handed to `infer_model` via the same `provider_factory` hook `ProviderSession.model_for`
    uses — `infer_model` still performs the production prefix→model-class dispatch."""
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        base_url=BASE_URL,
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)),
    )
    return infer_model(f"openai-{api}:gpt-4o", provider_factory=lambda _name: provider)


# ── §1 construction + custom base_url ────────────────────────────────────────────────────


@pytest.mark.parametrize("api", APIS)
def test_real_construction_dispatches_to_the_api_specific_model_class(api, stub):
    model = _real_model(api, stub)
    expected = {"chat": "OpenAIChatModel", "responses": "OpenAIResponsesModel"}[api]
    assert type(model).__name__ == expected
    assert model.model_name == "gpt-4o"


@pytest.mark.parametrize("api", APIS)
def test_custom_base_url_routes_to_the_api_specific_path(api, stub):
    agent = Agent(_real_model(api, stub), output_type=str)
    result = asyncio.run(agent.run("hello"))
    assert result.output == _VERDICT_JSON
    assert stub.seen, "no request reached the stub"
    url = stub.seen[0].url
    assert url.host == "stub.localdomain", "request escaped the custom base_url"
    assert url.path == EXPECTED_PATH[api]


# ── §2 structured output ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("api", APIS)
def test_structured_output_round_trips(api, stub):
    from pydantic_ai import PromptedOutput

    agent = Agent(_real_model(api, stub), output_type=PromptedOutput(Verdict))
    result = asyncio.run(agent.run("verdict please"))
    assert result.output == Verdict(verdict="PASS", summary="stub answered")


# ── §3 streaming ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("api", APIS)
def test_streaming_issues_a_streaming_request_and_yields_the_text(api, stub):
    agent = Agent(_real_model(api, stub), output_type=str)

    async def _run() -> str:
        async with agent.run_stream("stream please") as stream:
            return await stream.get_output()

    assert asyncio.run(_run()) == _VERDICT_JSON
    assert _chat_body(stub.seen[0]).get("stream") is True


# ── §4 tool calls ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("api", APIS)
def test_tool_call_turn_executes_the_tool_then_finishes(api, stub):
    calls: list[str] = []
    agent = Agent(_real_model(api, stub), output_type=str)

    @agent.tool_plain
    def lookup() -> str:
        calls.append("lookup")
        return "found"

    result = asyncio.run(agent.run("use the tool"))
    assert calls == ["lookup"], "the tool the stub requested was not executed"
    assert result.output == _VERDICT_JSON
    assert len(stub.seen) == 2, "expected a tool-call turn then a final turn"


# ── §5 retry surface ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("api", APIS)
def test_a_429_surfaces_as_model_http_error_after_the_sdk_retries(api, stub):
    stub.status_code = 429
    agent = Agent(_real_model(api, stub), output_type=str)
    with pytest.raises(ModelHTTPError) as exc_info:
        asyncio.run(agent.run("hello"))
    assert exc_info.value.status_code == 429
    # Where the retry actually lives — and that it is IDENTICAL across both APIs: pydantic-ai's
    # model layer adds no retry, but the openai SDK client underneath retries a 429 twice
    # (AsyncOpenAI's max_retries default of 2), so exactly 3 requests reach the wire before the
    # error surfaces. A migration must not assume the Responses path changes this.
    assert len(stub.seen) == 3
