"""Expose the RP-04 S4 provider-native authentication happy path.

``LLMRuntime`` carries provider-specific credentials outside ``LLMConfig``.
``ProviderSession``, ``PydanticAIRunner``, and ``get_runner`` thread the optional
carrier to the existing provider builder. A missing runtime preserves ambient
construction, and only the selected provider is consumed.

Held-out oracles merged from test_rp04_s4_llm_runtime_heldout.py: Explicit empty or conflicting
credentials fail before client construction. Only the selected provider consumes its carrier,
secret material stays out of representations and errors, and AST checks prevent duplicate client,
agent, or structured-operation owners.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import ClassVar

import pytest

from rebar.llm.auth import AnthropicAuth, BedrockAuth, LLMRuntime, OpenAIAuth
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

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

    monkeypatch.setattr(
        pai_bedrock,
        "BedrockProvider",
        lambda **kw: SimpleNamespace(model_profile=lambda model_name: None, **kw),
    )

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


# ── held-out oracles (merged from test_rp04_s4_llm_runtime_heldout.py) ──

_SENTINEL = "s3cr3t-PAT-do-not-leak-42"


# ── AC4 — conflicting/empty explicit auth fails closed BEFORE a provider request ──────── #
def test_conflicting_anthropic_auth_fails_before_client_build(monkeypatch):
    """Both api_key AND auth_token supplied is a misconfiguration; it must raise a typed
    error BEFORE AsyncAnthropic is ever constructed (no anonymous/ambient fallback)."""
    import anthropic

    class _Boom:
        def __init__(self, **_kw):
            raise AssertionError("AsyncAnthropic constructed despite conflicting auth")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Boom)

    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(api_key="a", auth_token="b"))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        with pytest.raises(LLMConfigError):
            session.provider_factory("anthropic")


def test_empty_anthropic_carrier_fails_closed(monkeypatch):
    """A SUPPLIED anthropic carrier with nothing set is an explicit-but-empty auth: it must
    fail closed rather than silently degrade to the ambient environment credential."""
    import anthropic

    class _Boom:
        def __init__(self, **_kw):
            raise AssertionError("AsyncAnthropic constructed despite empty explicit carrier")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Boom)

    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth())
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        with pytest.raises(LLMConfigError):
            session.provider_factory("anthropic")


# ── AC4/AC5 — only the SELECTED provider's carrier is consumed, no cross-provider ─────── #
def test_only_selected_provider_carrier_consumed(monkeypatch):
    """A runtime carrying all three providers, building anthropic, must not touch the
    bedrock or openai carriers (no cross-provider construction)."""
    import anthropic
    import pydantic_ai.providers.anthropic as pai_anthropic

    seen = {}

    class _Fake:
        def __init__(self, **kw):
            seen.update(kw)
            self.max_retries = 0

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Fake)
    monkeypatch.setattr(pai_anthropic, "AnthropicProvider", lambda **kw: SimpleNamespace(**kw))

    def _session_boom():
        raise AssertionError("bedrock session touched while building anthropic")

    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(
        anthropic=AnthropicAuth(api_key="k"),
        bedrock=BedrockAuth(session=SimpleNamespace(client=lambda *a, **k: _session_boom())),
        openai=OpenAIAuth(api_key="should-not-be-used"),
    )
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        session.provider_factory("anthropic")
    assert seen.get("api_key") == "k"


# ── AC5 — secret canaries absent from every named boundary ────────────────────────────── #
def test_secret_absent_from_runtime_config_and_session_repr():
    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(api_key=_SENTINEL))
    from rebar.llm.providers import ProviderSession

    session = ProviderSession(cfg, runtime=runtime)
    for blob in (
        repr(runtime),
        str(runtime),
        repr(runtime.anthropic),
        repr(cfg),
        repr(session),
    ):
        assert _SENTINEL not in blob


def test_secret_absent_from_conflict_error_message(monkeypatch):
    """A fail-closed error must not echo the secret material it rejected."""
    import anthropic

    class _Boom:
        def __init__(self, **_kw):
            raise AssertionError("AsyncAnthropic constructed despite conflicting auth")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Boom)
    cfg = _cfg(model="claude-sonnet-4-6", model_provider="anthropic")
    runtime = LLMRuntime(anthropic=AnthropicAuth(api_key=_SENTINEL, auth_token=_SENTINEL + "b"))
    from rebar.llm.providers import ProviderSession

    with ProviderSession(cfg, runtime=runtime) as session:
        with pytest.raises(LLMConfigError) as exc:
            session.provider_factory("anthropic")
    assert _SENTINEL not in str(exc.value)


# ── AC5 — secret is not serialized into LLMConfig / snapshot surfaces ─────────────────── #
def test_runtime_is_not_carried_on_llmconfig():
    """The LLMRuntime (and its secrets) must live OUTSIDE LLMConfig, so config snapshots /
    fingerprints / caches can never carry the credential."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(LLMConfig)}
    assert "runtime" not in field_names
    assert not any("runtime" in n.lower() for n in field_names)


# ── AC3 — structural: no second client owner / Agent route / _pai_structured caller ──── #
def test_auth_module_adds_no_second_agent_or_structured_owner():
    """The auth carrier is pure data + validation — it must not CALL an Agent constructor, the
    single authoritative structured op, a provider SDK client, or boto3. Asserted against the
    parsed AST (real call/attribute sites) so a docstring or comment that merely *names* one of
    these — explaining what auth.py deliberately does not do — is not a false positive."""
    import ast

    import rebar.llm.auth as auth_mod

    source = pathlib.Path(auth_mod.__file__).read_text()
    tree = ast.parse(source)

    called_names: set[str] = set()
    attr_paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attr_paths.add(f"{node.value.id}.{node.attr}")

    banned_calls = {
        "Agent",
        "_pai_structured",
        "AsyncAnthropic",
        "OpenAIProvider",
        "BedrockProvider",
    }
    offending = banned_calls & called_names
    assert not offending, f"auth.py must not call {sorted(offending)}"
    assert not any(p.startswith("boto3.") for p in attr_paths), (
        "auth.py must not use boto3 directly"
    )
