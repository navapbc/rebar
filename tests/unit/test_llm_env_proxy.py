"""rebar's LLM HTTP clients honour the standard proxy environment (bug chilled-rubellite-bluewhale).

rebar builds its LLM clients with an explicit httpx transport, and httpx applies
``HTTPS_PROXY``/``HTTP_PROXY``/``ALL_PROXY``/``NO_PROXY`` only when no transport is passed. On a
host whose egress is proxy-only every gate call then failed with a DNS error. These tests
replace ``httpx.AsyncHTTPTransport`` (the existing provider seam) with a recording factory, so
each request reports which transport handled it and which ``proxy=`` that transport was built
with. They run offline: ``httpx.MockTransport`` answers every request.
"""

from __future__ import annotations

import asyncio
import os

import anthropic
import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("tenacity")

from rebar.llm.anthropic_model import _build_retrying_anthropic_provider
from rebar.llm.config import LLMConfig

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("clean_proxy_env")]

_PROXY = "http://proxy.example:3128"
_OTHER_PROXY = "http://all-proxy.example:8080"
_DIRECT = None  # the recorder's label for a transport built without ``proxy=``


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """Start every test with no proxy variable at all, whatever the host shell exports
    (the Claude Code sandbox exports several), so each test sets exactly what it asserts on."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "REQUEST_METHOD":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


class _Recorder:
    """Stands in for ``<http module>.AsyncHTTPTransport``. Every transport it builds answers
    200 and logs ``(url, proxy-it-was-built-with)`` for each request it handles."""

    def __init__(self, http_module, body: dict | None = None) -> None:
        self._http = http_module
        self._body = body if body is not None else _ok_message()
        self.built: list[str | None] = []
        self.handled: list[tuple[str, str | None]] = []

    def __call__(self, *args, **kwargs):
        proxy = kwargs.get("proxy")
        if proxy is not None and str(proxy).startswith("ftp://"):
            # What the real transport does with a scheme it cannot proxy through.
            raise ValueError(f"Unknown scheme for proxy URL {proxy!r}")
        label = None if proxy is None else str(proxy)
        self.built.append(label)

        def _handler(request):
            self.handled.append((str(request.url), label))
            return self._http.Response(200, json=self._body)

        return self._http.MockTransport(_handler)


def _ok_message() -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _sdk_http_module():
    """The HTTP module the installed Anthropic SDK takes its client from (httpx or httpx2)."""
    from rebar.llm.anthropic_model import _anthropic_http_client_module

    return _anthropic_http_client_module(anthropic.AsyncAnthropic)


@pytest.fixture
def recorder(monkeypatch):
    http_module = _sdk_http_module()
    rec = _Recorder(http_module)
    monkeypatch.setattr(http_module, "AsyncHTTPTransport", rec)
    return rec


def _send_through_anthropic_client(*urls: str) -> None:
    _provider, http_client = _build_retrying_anthropic_provider(
        base_url=None, cfg=LLMConfig(repo_path=".")
    )

    async def _go():
        try:
            for url in urls:
                response = await http_client.get(url)
                assert response.status_code == 200
        finally:
            await http_client.aclose()

    asyncio.run(_go())


# ── Anthropic provider client ─────────────────────────────────────────────────────────


def test_anthropic_client_routes_through_https_proxy(monkeypatch, recorder):
    """AC1: with ``HTTPS_PROXY`` set, the request to the Anthropic API leaves through it."""
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)

    _send_through_anthropic_client("https://api.anthropic.com/v1/messages")

    assert recorder.handled == [("https://api.anthropic.com/v1/messages", _PROXY)]


def test_anthropic_client_no_proxy_host_connects_directly(monkeypatch, recorder):
    """AC2: a host matched by ``NO_PROXY`` goes direct; the choice is per request URL, so an
    unmatched host in the same client still uses the proxy."""
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost,api.anthropic.com")

    _send_through_anthropic_client(
        "https://api.anthropic.com/v1/messages", "https://gateway.example/v1/messages"
    )

    assert recorder.handled == [
        ("https://api.anthropic.com/v1/messages", _DIRECT),
        ("https://gateway.example/v1/messages", _PROXY),
    ]


def test_anthropic_client_without_proxy_variables_is_unchanged(recorder):
    """AC2: no proxy variable set -> exactly the one direct transport built today."""
    _send_through_anthropic_client("https://api.anthropic.com/v1/messages")

    assert recorder.built == [_DIRECT]
    assert recorder.handled == [("https://api.anthropic.com/v1/messages", _DIRECT)]


def test_anthropic_client_falls_back_to_all_proxy(monkeypatch, recorder):
    """``ALL_PROXY`` applies when the scheme's own variable is absent, as in httpx."""
    monkeypatch.setenv("ALL_PROXY", _OTHER_PROXY)

    _send_through_anthropic_client("https://api.anthropic.com/v1/messages")

    assert recorder.handled == [("https://api.anthropic.com/v1/messages", _OTHER_PROXY)]


def test_explicit_wrapped_transport_is_used_verbatim(monkeypatch, recorder):
    """The ``_wrapped_transport`` test seam still bypasses proxy selection entirely."""
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    http_module = _sdk_http_module()
    seen: list[str] = []

    def _handler(request):
        seen.append(str(request.url))
        return http_module.Response(200, json=_ok_message())

    _provider, http_client = _build_retrying_anthropic_provider(
        base_url=None,
        cfg=LLMConfig(repo_path="."),
        _wrapped_transport=http_module.MockTransport(_handler),
    )

    async def _go():
        try:
            await http_client.get("https://api.anthropic.com/v1/messages")
        finally:
            await http_client.aclose()

    asyncio.run(_go())

    assert seen == ["https://api.anthropic.com/v1/messages"]
    assert recorder.built == []


def test_httpx2_transport_routes_through_proxy(monkeypatch):
    """The httpx2 branch gets the same proxy-aware transport inside its retry wrapper."""
    httpx2 = pytest.importorskip("httpx2")
    from rebar.llm.anthropic_model import (
        _build_httpx2_tenacity_transport,
        _env_proxy_aware_transport,
    )

    rec = _Recorder(httpx2)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", rec)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    monkeypatch.setenv("NO_PROXY", "direct.example")

    transport = _build_httpx2_tenacity_transport(
        httpx2,
        config={"reraise": True},
        wrapped=_env_proxy_aware_transport(httpx2),
    )
    client = httpx2.AsyncClient(transport=transport)

    async def _go():
        try:
            await client.get("https://api.anthropic.com/v1/messages")
            await client.get("https://direct.example/x")
        finally:
            await client.aclose()

    asyncio.run(_go())

    assert rec.handled == [
        ("https://api.anthropic.com/v1/messages", _PROXY),
        ("https://direct.example/x", _DIRECT),
    ]


def test_unusable_proxy_value_is_skipped_with_a_warning(monkeypatch, recorder, caplog):
    """A proxy value no transport can be built for (today ignored by rebar) must not break
    every call: it is skipped with a warning and the request falls back as if it were unset."""
    monkeypatch.setenv("ALL_PROXY", "ftp://user:secret@ftp-proxy.example:21")

    with caplog.at_level("WARNING", logger="rebar.llm.anthropic_model"):
        _send_through_anthropic_client("https://api.anthropic.com/v1/messages")

    assert recorder.handled == [("https://api.anthropic.com/v1/messages", _DIRECT)]
    warnings = [r.getMessage() for r in caplog.records if "proxy" in r.getMessage().lower()]
    assert warnings, "the skipped proxy must be reported"
    assert all("secret" not in w and "ftp-proxy.example" not in w for w in warnings)


def test_unusable_all_proxy_does_not_disable_a_usable_scheme_proxy(monkeypatch, recorder):
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    monkeypatch.setenv("ALL_PROXY", "ftp://ftp-proxy.example:21")

    _send_through_anthropic_client("https://api.anthropic.com/v1/messages")

    assert recorder.handled == [("https://api.anthropic.com/v1/messages", _PROXY)]


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:8443/v1/messages",
        "https://127.0.0.1:8443/v1/messages",
        "https://[::1]:8443/v1/messages",
        "https://llm.localhost/v1/messages",
    ],
)
def test_loopback_hosts_connect_directly_without_no_proxy(monkeypatch, recorder, url):
    """A proxy cannot reach this machine's loopback, so a loopback endpoint goes direct even
    when ``NO_PROXY`` does not list it."""
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)

    _send_through_anthropic_client(url)

    assert [proxy for _, proxy in recorder.handled] == [_DIRECT]


# ── OpenAI-compatible client ──────────────────────────────────────────────────────────


def _run_openai_compatible(monkeypatch, base_url: str) -> _Recorder:
    import pydantic_ai.models

    from rebar.llm.runner import PydanticAIRunner, RunRequest

    rec = _Recorder(httpx, body=_chat_completion())
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", rec)
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)
    cfg = LLMConfig(
        repo_path=".",
        model="local-model",
        model_provider="openai",
        base_url=base_url,
        api_key="not-needed",
    )
    request = RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="structured",
        output_schema="completion_verdict",
        execution_mode="single_turn",
    )
    out = PydanticAIRunner(cfg).run(request)
    assert out["verdict"] == "PASS"
    return rec


def _chat_completion() -> dict:
    content = '{"verdict": "PASS", "findings": [], "summary": "ok"}'
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 0,
        "model": "local-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_openai_compatible_client_routes_through_http_proxy(monkeypatch):
    """AC3: the OpenAI-compatible client honours ``HTTP_PROXY`` for an ``http://`` endpoint."""
    monkeypatch.setenv("HTTP_PROXY", _PROXY)

    rec = _run_openai_compatible(monkeypatch, "http://llm.example:1234/v1")

    assert rec.handled
    assert all(url.startswith("http://llm.example:1234/v1/") for url, _ in rec.handled)
    assert {proxy for _, proxy in rec.handled} == {_PROXY}


def test_openai_compatible_client_no_proxy_host_connects_directly(monkeypatch):
    """AC3: a ``NO_PROXY`` local server is reached directly even with a proxy configured."""
    monkeypatch.setenv("HTTP_PROXY", _PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    rec = _run_openai_compatible(monkeypatch, "http://localhost:1234/v1")

    assert rec.handled
    assert {proxy for _, proxy in rec.handled} == {_DIRECT}


def test_openai_compatible_local_server_connects_directly_without_no_proxy(monkeypatch):
    """A local server keeps working when a proxy variable is set without ``NO_PROXY``."""
    monkeypatch.setenv("HTTP_PROXY", _PROXY)

    rec = _run_openai_compatible(monkeypatch, "http://localhost:1234/v1")

    assert rec.handled
    assert {proxy for _, proxy in rec.handled} == {_DIRECT}


def test_openai_compatible_client_without_proxy_variables_is_unchanged(monkeypatch):
    """AC3: no proxy variable set -> one direct transport, as before."""
    rec = _run_openai_compatible(monkeypatch, "http://localhost:1234/v1")

    assert rec.built == [_DIRECT]
    assert {proxy for _, proxy in rec.handled} == {_DIRECT}
