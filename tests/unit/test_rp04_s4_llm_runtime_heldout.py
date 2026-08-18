"""RP-04 S4 (bbdc) HELD-OUT oracle — provider-native LLM auth injection.

Withheld from the implementer. Asserts the edge/fail-closed/secret contracts of
``rebar.llm.auth.LLMRuntime`` as OBSERVABLE behavior — a supplied-but-conflicting
carrier fails BEFORE any client/model request, secret sentinels never appear on any
named boundary, and exactly the selected provider's carrier is consumed with no
ambient/alternate-principal fallback. Structural guard: auth injection adds no second
client owner, Agent route, ``_pai_structured`` caller, or bespoke retry scheduler.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from rebar.llm.auth import AnthropicAuth, BedrockAuth, LLMRuntime, OpenAIAuth
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

pytestmark = pytest.mark.unit

_SENTINEL = "s3cr3t-PAT-do-not-leak-42"


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


# ── AC4 — conflicting/empty explicit auth fails closed BEFORE a provider request ──────── #
def test_conflicting_anthropic_auth_fails_before_client_build(monkeypatch):
    """Both api_key AND auth_token supplied is a misconfiguration; it must raise a typed
    error BEFORE AsyncAnthropic is ever constructed (no anonymous/ambient fallback)."""
    import anthropic

    def _boom(**_kw):
        raise AssertionError("AsyncAnthropic constructed despite conflicting auth")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _boom)

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

    def _boom(**_kw):
        raise AssertionError("AsyncAnthropic constructed despite empty explicit carrier")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _boom)

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

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **k: None)
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
