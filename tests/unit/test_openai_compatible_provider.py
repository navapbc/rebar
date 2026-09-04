"""Best-effort OpenAI-compatible endpoint: honor REBAR_LLM_BASE_URL (story S4).

`_pai_check_config` used to REFUSE any `base_url`/`api_key` outright, while
`docs/llm-framework.md` documented both as working and printed an LMStudio/Ollama recipe, and
`config.py` already parsed them. The documentation was false. This pins the behaviour that
makes it true.

Contract tier: the defect class is a consumer (the runner) reading configuration the wrong
way, so these drive real config resolution and the real provider seam rather than calling the
builder directly. Everything is real except the socket.

Each test names the acceptance criterion it discharges, because story f184 closed late when
half a criterion turned out to have no test despite an 18-test oracle — mutation testing
verifies the tests you have, not the test you never wrote.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("openai")

import pydantic_ai.models
from pydantic_ai import NativeOutput, PromptedOutput

from rebar.llm import structured
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_VERDICT_JSON = '{"verdict": "PASS", "findings": [], "summary": "local server answered"}'


class _Verdict:
    """Stand-in output model; ``output_mode`` only needs a class to wrap."""


def _chat_completion(content: str) -> dict:
    """A minimal OpenAI chat-completions response — the wire shape every
    OpenAI-compatible server (LMStudio / Ollama / vLLM / a LiteLLM proxy) speaks."""
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


@pytest.fixture
def stub_openai_server(monkeypatch):
    """A stub OpenAI-compatible server, reached through the REAL builder.

    Patching ``httpx.AsyncHTTPTransport`` (the pattern the provider-seam tests already use)
    means the production ``OpenAIProvider``/``OpenAIChatModel`` are genuinely constructed and
    only the socket is faked. Records every request so the test can assert which endpoint was
    actually called."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_completion(_VERDICT_JSON))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **kw: httpx.MockTransport(_handler))
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)
    return seen


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


def _req(cfg) -> RunRequest:
    return RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="structured",
        output_schema="completion_verdict",
        execution_mode="single_turn",
    )


# ── §A happy path ───────────────────────────────────────────────────────────────────────


def test_documented_local_server_recipe_returns_a_structured_verdict(stub_openai_server):
    """AC1 — the exact recipe printed at ``docs/llm-framework.md:131-133`` works end to end.

    That recipe is ``REBAR_LLM_MODEL=local-model REBAR_LLM_MODEL_PROVIDER=openai
    REBAR_LLM_BASE_URL=http://localhost:1234/v1 REBAR_LLM_API_KEY=not-needed``. It is
    documented as working and currently raises, which is the falsehood this story fixes."""
    cfg = _cfg(
        model="local-model",
        model_provider="openai",
        base_url="http://localhost:1234/v1",
        api_key="not-needed",
    )
    out = PydanticAIRunner(cfg).run(_req(cfg))

    assert out["verdict"] == "PASS"
    assert out["summary"] == "local server answered"
    # The call really went to the configured endpoint, not to api.openai.com.
    assert stub_openai_server, "no request was made"
    assert "localhost:1234" in str(stub_openai_server[0].url)


def test_check_config_accepts_a_valid_base_url():
    """AC3a — the loud refusal is gone: a well-formed base_url no longer raises."""
    from rebar.llm.runner import _pai_check_config

    _pai_check_config(
        _cfg(model_provider="openai", base_url="http://localhost:1234/v1", api_key="k")
    )


def _model_through_the_real_seam(cfg):
    """Build the model exactly as ``run()`` does: the rebar factory supplies the Provider and
    pydantic-ai's ``infer_model`` builds the model from it. Asserting on the result of THIS
    path is what makes these contract tests rather than assertions about a hand-built object."""
    from pydantic_ai.models import infer_model

    from rebar.llm.anthropic_model import _pai_model
    from rebar.llm.providers import ProviderSession

    session = ProviderSession(cfg)
    with session:
        return infer_model(_pai_model(cfg), provider_factory=session.provider_factory)


def test_custom_endpoint_withdraws_native_structured_output(stub_openai_server):
    """AC5 — an opaque endpoint must take the PROMPTED path.

    An OpenAI-*compatible* server has no obligation to implement strict ``json_schema``
    constrained decoding, and upstream's profile describes OpenAI's HOSTED API, so the builder
    withdraws that one flag. Without this the endpoint gets NativeOutput and fails opaquely.

    The withdrawal must ride the PROVIDER, not a model argument: pydantic-ai resolves
    ``profile=profile or provider.model_profile``, and rebar's seam hands back a Provider, so
    the Provider is the only place the override can be attached and still survive
    ``infer_model``."""
    from rebar.llm.capabilities import capabilities_for

    cfg = _cfg(model="local-model", model_provider="openai", base_url="http://localhost:1234/v1")
    model = _model_through_the_real_seam(cfg)

    assert model.profile.supports_json_schema_output is False
    caps = capabilities_for(model)
    assert caps.native_structured_output is False
    assert isinstance(structured.output_mode(_Verdict, caps), PromptedOutput)


# ── §B held out from the implementer ────────────────────────────────────────────────────


def test_base_url_does_not_hijack_provider_selection(stub_openai_server):
    """AC2 — the PR #121 regression this story exists to prevent.

    ``base_url`` is provider CONFIGURATION, never provider SELECTION. With
    ``REBAR_LLM_MODEL_PROVIDER=anthropic`` set explicitly, a base_url must not silently
    reroute the run to an OpenAI-shaped provider."""
    cfg = _cfg(
        model="claude-opus-4-8",
        model_provider="anthropic",
        base_url="http://localhost:1234/v1",
    )
    from rebar.llm.anthropic_model import _pai_model

    assert _pai_model(cfg).startswith("anthropic:"), (
        "base_url must not change which provider the model string selects"
    )


def test_api_key_without_base_url_is_a_config_error():
    """AC3b — ambiguous: the direct OpenAI path reads OPENAI_API_KEY, so a bare api_key with
    no endpoint is a mistake rather than a silent no-op."""
    from rebar.llm.runner import _pai_check_config

    with pytest.raises(LLMConfigError) as excinfo:
        _pai_check_config(_cfg(api_key="sk-orphan"))
    assert "base_url" in str(excinfo.value)


def test_non_absolute_base_url_is_a_config_error():
    """AC3c — a relative URL cannot be dialled; the error must name the variable so the
    operator knows which knob is wrong."""
    from rebar.llm.runner import _pai_check_config

    with pytest.raises(LLMConfigError) as excinfo:
        _pai_check_config(_cfg(model_provider="openai", base_url="localhost:1234/v1"))
    message = str(excinfo.value)
    assert "base_url" in message or "REBAR_LLM_BASE_URL" in message


def test_missing_openai_extra_raises_naming_the_install_command(monkeypatch):
    """AC3d — the opt-in extra's absence must be actionable, not an opaque ImportError from
    deep inside pydantic-ai."""
    import sys

    from rebar.llm.providers import ProviderSession

    monkeypatch.setitem(sys.modules, "pydantic_ai.providers.openai", None)
    cfg = _cfg(model="local-model", model_provider="openai", base_url="http://localhost:1234/v1")
    with ProviderSession(cfg) as session:
        with pytest.raises(LLMConfigError) as excinfo:
            session.provider_factory("openai-chat")
    assert "openai" in str(excinfo.value)


def test_no_strict_json_schema_is_sent_on_the_wire_to_a_custom_endpoint(stub_openai_server):
    """AC5, pinned at the BOUNDARY rather than on an object.

    A capability assertion on the model object can pass while production still selects
    NativeOutput — the runner has to pass the model OBJECT to ``capabilities_for()``, and if it
    ever regressed to passing the bare model STRING, the string path would re-resolve the
    vendor's DEFAULT profile (``supports_json_schema_output=True``) and the override would be
    silently ignored. An object-level assertion cannot see that.

    So assert what leaves the process: with PromptedOutput no strict ``json_schema``
    ``response_format`` is sent. This is the observable consequence for the local server, and
    it fails if the production path stops honouring the override for ANY reason."""
    import json

    cfg = _cfg(
        model="local-model",
        model_provider="openai",
        base_url="http://localhost:1234/v1",
        api_key="not-needed",
    )
    out = PydanticAIRunner(cfg).run(_req(cfg))
    assert out["verdict"] == "PASS"

    assert stub_openai_server, "no request reached the stub server"
    body = json.loads(stub_openai_server[0].content or b"{}")
    response_format = body.get("response_format")
    assert response_format is None or response_format.get("type") != "json_schema", (
        "a strict json_schema response_format was sent to an OpenAI-COMPATIBLE endpoint; "
        f"the profile override is not reaching the production output-mode decision "
        f"(response_format={response_format!r})"
    )


def test_profile_override_is_surgical(stub_openai_server):
    """AC6 — the override withdraws ONLY the strict-output claim.

    A blanket conservative record would silently disable unrelated capabilities (tool calling
    is the one that would break gate operations outright), so this asserts another flag read
    off the same profile is untouched — through the real seam, not a hand-built model."""
    from pydantic_ai.profiles.openai import openai_model_profile

    upstream = openai_model_profile("local-model")
    assert upstream.supports_json_schema_output is True  # premise
    assert upstream.supports_tools is True  # premise

    cfg = _cfg(model="local-model", model_provider="openai", base_url="http://localhost:1234/v1")
    model = _model_through_the_real_seam(cfg)

    assert model.profile.supports_json_schema_output is False
    assert model.profile.supports_tools is True, (
        "the override must be surgical — unrelated capabilities keep their upstream values"
    )


def test_bare_openai_string_without_base_url_builds_no_provider():
    """AC7 — regression guard for the eager-construction hazard.

    With no base_url rebar has nothing to inject, so it must NOT interpose a builder: the
    model stays a lazy STRING on S1's deferred path. Registering unconditionally would make
    the opt-in openai SDK a de facto requirement and would break the existing parameterized
    ``openai-chat:gpt-4o`` case that stubs ``_pai_structured`` so no provider is constructed."""
    from rebar.llm.providers import ProviderSession

    cfg = _cfg(model="gpt-4o", model_provider="openai")  # NO base_url
    with ProviderSession(cfg) as session:
        assert session.supports("openai-chat") is False, (
            "without base_url the openai-chat builder must not be registered"
        )
        assert session.is_resolvable("openai-chat") is False, (
            "hosted openai-chat must not delegate to pydantic-ai's Chat provider after removal"
        )
    assert list(getattr(session, "_closeables", [])) == []


def test_non_loopback_anthropic_base_url_is_respected_as_a_gateway(monkeypatch):
    """AC4 — pins the accidental capability so it cannot change silently.

    ``_local_proxy_bypass_base_url`` overrides ONLY loopback hosts, so a real gateway host in
    ``ANTHROPIC_BASE_URL`` is respected and rebar already talks Anthropic-wire through it.
    That works today by accident; this makes it deliberate."""
    from rebar.llm.anthropic_model import _local_proxy_bypass_base_url

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com/v1")
    monkeypatch.delenv("REBAR_LLM_ALLOW_LOCAL_PROXY", raising=False)
    assert _local_proxy_bypass_base_url() is None, (
        "a non-loopback gateway must be respected, not bypassed"
    )

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8080")
    assert _local_proxy_bypass_base_url() == "https://api.anthropic.com", (
        "a loopback proxy must still be bypassed (bug sue-skimp-tear)"
    )


def test_native_output_still_selected_for_a_hosted_openai_model():
    """Contrast case — the override must not leak to the hosted API. A plain ``openai:gpt-4o``
    (no custom endpoint) keeps NativeOutput, proving the withdrawal is scoped to opaque
    endpoints rather than applied to the whole provider family."""
    from rebar.llm.capabilities import capabilities_for

    caps = capabilities_for("openai:gpt-4o")
    assert caps.native_structured_output is True
    assert isinstance(structured.output_mode(_Verdict, caps), NativeOutput)


# ── bug 6e70: a per-class endpoint must reach the local builder for the PRIMARY model ──────


def _configure_class_endpoint(
    monkeypatch,
    *,
    cls="trivial",
    model="local-model",
    provider="openai",
    endpoint="http://localhost:1234/v1",
):
    """Point a model-class slot at a local ``endpoint`` through the REAL config path
    (``load_class_slots`` -> ``_read_llm_file_table``) — the same seam the
    ``REBAR_LLM_<CLASS>_ENDPOINT`` env var and the ``[tool.rebar.llm.model_classes]`` TOML land
    on — so the endpoint has to survive parsing to reach the runner."""
    from rebar.llm import config as llm_config

    table = {"model_classes": {cls: {"model": model, "provider": provider, "endpoint": endpoint}}}
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: table)


def test_per_class_endpoint_routes_the_primary_through_the_local_builder(
    stub_openai_server, monkeypatch
):
    """Regression for bug 6e70 — a model class configured with a local ``endpoint``
    (``REBAR_LLM_<CLASS>_ENDPOINT`` / the slot ``endpoint`` field) and NO top-level ``base_url``
    must reach the local server through rebar's OpenAI-compatible builder, not fall through to
    pydantic-ai's stock ``OpenAIProvider`` (which raises 'Missing credentials').

    The slot ``endpoint`` was parsed, given dedicated env vars, and documented, but the only
    consumer of any ``.endpoint`` was the FALLBACK chain — the PRIMARY model silently dropped it.
    Ops collapse the class onto ``cfg.model`` via ``resolve_model_string``; the endpoint stays in
    the slot config, never on ``cfg``, so the runner must recover it from the resolved model."""
    _configure_class_endpoint(monkeypatch)

    from rebar.llm.model_classes import TRIVIAL_CLASS, resolve_model_string

    cfg = _cfg(model=resolve_model_string(TRIVIAL_CLASS))
    assert cfg.base_url is None, "the endpoint lives in the slot, not on cfg — that is the bug"

    out = PydanticAIRunner(cfg).run(_req(cfg))

    assert out["verdict"] == "PASS"
    assert stub_openai_server, "no request reached the configured per-class endpoint"
    assert "localhost:1234" in str(stub_openai_server[0].url)
