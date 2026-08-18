"""RP-04 S4 (bbdc) HAPPY-PATH oracle — provider-native LLM auth injection.

Visible to the implementer. Pins the ``rebar.llm.auth.LLMRuntime`` contract and
proves the native pre-client capability reaches EXACTLY the existing per-provider
``ProviderSession`` builder — observable through the client/provider each builder
constructs, not private structure. Edge/fail-closed/secret-canary cases are held
out (see the withheld oracle).

Contract (the new seam):
    rebar.llm.auth.LLMRuntime(anthropic=..., bedrock=..., openai=...)
      - AnthropicAuth(api_key=..., auth_token=...)
      - BedrockAuth(session=<boto3.Session>)
      - OpenAIAuth(api_key=<str | callable>)
    ProviderSession(cfg, *, runtime=None)         # threads the runtime
    PydanticAIRunner(config, *, model_override=None, runtime=None)
    get_runner(config, *, runtime=None, override=None)
      - runtime=None  -> byte-identical RP-01 ambient construction (compat)
      - only the SELECTED provider's carrier is consumed
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

from rebar.llm.auth import AnthropicAuth, BedrockAuth, LLMRuntime, OpenAIAuth
from rebar.llm.config import LLMConfig

pytestmark = pytest.mark.unit


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


class _FakeAsyncAnthropic:
    """Captures the kwargs the builder hands ``AsyncAnthropic`` — the injection point."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = dict(kwargs)
        self.max_retries = kwargs.get("max_retries", 0)


@pytest.fixture
def capture_anthropic(monkeypatch):
    import anthropic
    import pydantic_ai.providers.anthropic as pai_anthropic

    _FakeAsyncAnthropic.last_kwargs = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    # Accept any client object so no real SDK validation runs — we assert on the
    # captured AsyncAnthropic kwargs, the observable injection contract.
    monkeypatch.setattr(pai_anthropic, "AnthropicProvider", lambda **kw: SimpleNamespace(**kw))
    return _FakeAsyncAnthropic


def test_anthropic_api_key_reaches_the_client(capture_anthropic):
    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(api_key="sk-happy-key"))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        session.provider_factory("anthropic")
    assert capture_anthropic.last_kwargs.get("api_key") == "sk-happy-key"


def test_anthropic_auth_token_reaches_the_client(capture_anthropic):
    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(auth_token="oauth-happy-token"))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        session.provider_factory("anthropic")
    assert capture_anthropic.last_kwargs.get("auth_token") == "oauth-happy-token"


def test_runtime_none_preserves_ambient_anthropic(capture_anthropic):
    """Compat: no runtime -> the builder passes NEITHER native key kwarg, so the SDK
    resolves its ambient credential exactly as before RP-04."""
    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg) as session:
        session.provider_factory("anthropic")
    assert "api_key" not in capture_anthropic.last_kwargs
    assert "auth_token" not in capture_anthropic.last_kwargs


def test_bedrock_injected_session_is_used(monkeypatch):
    pytest.importorskip("boto3")
    import boto3

    class _FakeClient:
        pass

    used = {}

    class _FakeSession:
        def client(self, name, **kw):
            used["client_service"] = name
            return _FakeClient()

    # Ambient session construction must NOT happen when a session is injected.
    def _boom_session(*a, **k):
        raise AssertionError("ambient boto3.session.Session built despite injected session")

    monkeypatch.setattr(boto3.session, "Session", _boom_session)
    import pydantic_ai.providers.bedrock as pai_bedrock

    monkeypatch.setattr(pai_bedrock, "BedrockProvider", lambda **kw: SimpleNamespace(**kw))

    cfg = _cfg(model="us.anthropic.claude-sonnet-4-6", model_provider="bedrock")
    runtime = LLMRuntime(bedrock=BedrockAuth(session=_FakeSession()))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        provider = session.provider_factory("bedrock")
    assert used.get("client_service") == "bedrock-runtime"
    assert isinstance(provider.bedrock_client, _FakeClient)


def test_openai_api_key_reaches_the_provider(monkeypatch):
    pytest.importorskip("pydantic_ai.providers.openai")
    import pydantic_ai.providers.openai as pai_openai

    captured = {}

    class _FakeOpenAIProvider:
        def __init__(self, **kw):
            captured.update(kw)

        @staticmethod
        def model_profile(_name):
            return SimpleNamespace(supports_json_schema_output=True)

    monkeypatch.setattr(pai_openai, "OpenAIProvider", _FakeOpenAIProvider)

    cfg = _cfg(
        model="local-model",
        model_provider="openai",
        base_url="http://localhost:1234/v1",
    )
    runtime = LLMRuntime(openai=OpenAIAuth(api_key="sk-openai-happy"))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        session.provider_factory("openai")
    assert captured.get("api_key") == "sk-openai-happy"


def test_get_runner_threads_runtime_into_session(capture_anthropic):
    """get_runner(cfg, runtime=rt) -> a runner whose per-run ProviderSession carries rt."""
    from rebar.llm.runner import PydanticAIRunner, get_runner

    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(api_key="sk-threaded"))
    runner = get_runner(cfg, runtime=runtime)
    assert isinstance(runner, PydanticAIRunner)
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runner._runtime) as session:
        session.provider_factory("anthropic")
    assert capture_anthropic.last_kwargs.get("api_key") == "sk-threaded"
