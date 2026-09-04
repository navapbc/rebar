"""Contract: ``run()`` sources its provider through the ``ProviderSession`` seam (story S1).

This is a CONTRACT test, not a registry unit test. The provider factory is the producer and
``PydanticAIRunner.run()`` the consumer, so the assertions must be made on what ``run()``
actually builds and on what the session actually closes — a registry-only test cannot observe
either of the two defects this seam exists to prevent ("``run()`` bypasses the seam" and "the
session leaks the client it opened").

Every existing ``run()`` test drives ``model_override``, which BYPASSES provider construction
entirely, so nothing in the suite currently covers this path. Here everything is real except
the socket: the production Anthropic builder runs, wrapping a ``MockTransport``, so the real
``AsyncTenacityTransport`` / ``AsyncAnthropic`` / ``AnthropicModel`` objects are constructed and
the assertions land on real object state (``httpx.AsyncClient.is_closed``), never on call counts.

**Two kinds of test live here, deliberately.** S1 is a *pure relocation* of the Anthropic
construction path, so a behavioural test of that path passes both before and after the move —
it cannot be RED, and pretending otherwise would be dishonest:

- **§A characterization** — these PASS against the pre-move inline branch and must keep passing
  after it becomes a registry entry. They are the regression net that gives the story's "no
  behavior change" claim teeth: a botched relocation (a dropped tenacity transport, a leaked
  client, teardown moved into the success path) turns them red.
- **§B new contract** — the behavior that genuinely does not exist yet: the ``ProviderSession``
  seam itself and its typed failure for an unknown provider. These are RED now.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.retries import AsyncTenacityTransport

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


def _anthropic_expects_httpx2_client() -> bool:
    import inspect

    import anthropic

    http_client = inspect.signature(anthropic.AsyncAnthropic.__init__).parameters["http_client"]
    return "httpx2.AsyncClient" in str(http_client.annotation)


def _transport_http_module():
    if _anthropic_expects_httpx2_client():
        return pytest.importorskip("httpx2")
    return httpx


def _ok_body(text: str = "OK") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _err_body() -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}


@pytest.fixture(autouse=True)
def _anthropic_env(monkeypatch):
    """The real ``AsyncAnthropic`` builds auth headers before the MockTransport is reached, so
    it needs *a* key present. Never sent anywhere — no real request is made. ``ANTHROPIC_BASE_URL``
    is cleared so the loopback-bypass branch is not entered by a dev machine's ambient value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


@pytest.fixture
def seam(monkeypatch):
    """Real construction, mocked socket.

    - ``httpx.AsyncHTTPTransport`` -> ``MockTransport``: the production builder still wraps it in
      a real ``AsyncTenacityTransport``, so retry/timeout wiring is genuinely exercised.
    - ``httpx.AsyncClient`` -> a subclass that records every instance, so closure is asserted on
      the REAL client object rather than on a spy's call count.
    - ``runner.Agent`` -> a pass-through that captures the model ``run()`` handed its consumer.
      The real Agent still runs; this only observes what crossed the seam.
    """
    transport_http = _transport_http_module()
    clients: list = []
    real_client_cls = transport_http.AsyncClient
    status = {"code": 200}

    class _RecordingAsyncClient(real_client_cls):  # type: ignore[valid-type,misc]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            clients.append(self)

    def _handler(request):
        if status["code"] == 200:
            return transport_http.Response(200, json=_ok_body())
        return transport_http.Response(status["code"], json=_err_body())

    monkeypatch.setattr(transport_http, "AsyncClient", _RecordingAsyncClient)
    monkeypatch.setattr(
        transport_http,
        "AsyncHTTPTransport",
        lambda *a, **kw: transport_http.MockTransport(_handler),
    )

    import rebar.llm.runner as runner_mod

    captured: dict = {}
    real_import = runner_mod._import_pydantic_ai

    def _capturing_import():
        real_agent = real_import()

        def _agent(model, **kw):
            captured["model"] = model
            return real_agent(model, **kw)

        return _agent

    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", _capturing_import)
    # A MockTransport makes no real network call; the conftest socket guard still blocks any
    # accidental real connect.
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)

    return {"clients": clients, "captured": captured, "status": status}


def _retrying_clients(clients) -> list:
    """The clients the provider seam built — identified by the retrying transport the builder
    installs. Filters out any unrelated client another library may construct, so the negative
    control asserts on OUR construction rather than on a global count."""
    return [
        c
        for c in clients
        if isinstance(getattr(c, "_transport", None), AsyncTenacityTransport)
        or getattr(c, "_transport", None).__class__.__name__ == "_Httpx2AsyncTenacityTransport"
    ]


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    kw.setdefault("model", "anthropic:claude-sonnet-4-6")
    return LLMConfig(**kw)


def _req(cfg) -> RunRequest:
    # single_turn => tools=[] and toolsets=[], so the run needs no gate session and makes
    # exactly one model call through the seam under test.
    return RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="text",
        execution_mode="single_turn",
    )


# ── §A characterization: behavior that must survive the relocation unchanged ────────────


def test_run_builds_its_anthropic_model_through_the_provider_seam(seam):
    """``run()`` does not construct a provider inline: the model its consumer receives is a real
    ``AnthropicModel`` carrying the seam's retrying transport and the SDK-retries-disabled guard.

    Asserting on the constructed objects (not on a name-matched branch) keeps this true after the
    inline ``resolved.startswith("anthropic")`` branch is replaced by a registry lookup."""
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    model = seam["captured"].get("model")
    assert model is not None, "run() never handed a model to its Agent"
    assert type(model).__name__ == "AnthropicModel"
    # Retry is owned by the transport, never the SDK.
    assert model.client.max_retries == 0

    built = _retrying_clients(seam["clients"])
    assert len(built) == 1, f"expected exactly one seam-built client, got {len(built)}"


def test_session_closes_the_client_it_opened_after_a_successful_run(seam):
    """The session owns the client lifecycle: after ``run()`` returns normally, the real
    ``httpx.AsyncClient`` the seam opened reports closed."""
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    built = _retrying_clients(seam["clients"])
    assert len(built) == 1
    assert built[0].is_closed is True, "the seam-built client leaked after a successful run"


# ── §B new contract: the ProviderSession seam (RED before the story lands) ──────────────


def test_provider_session_exposes_a_pydantic_ai_compatible_factory(seam):
    """The seam's public contract, exercised exactly as ``infer_model`` will call it.

    ``infer_model`` invokes ``provider_factory(provider_name)`` with the BARE provider name
    (verified against pydantic-ai 1.107.1: ``provider = provider_factory(provider_name)``), so the
    callable must accept one positional ``str`` and return a pydantic-ai ``Provider``. The session
    also owns teardown of anything the builder opened, which is why it is a session and not a
    bare function.

    Closure is observed on the REAL client objects constructed during the block (captured by the
    ``seam`` fixture), never by reading a private attribute off the session — so a
    behavior-preserving rename of the session's internals cannot turn this into a vacuous pass."""
    from pydantic_ai.providers import Provider

    from rebar.llm.providers import ProviderSession

    cfg = _cfg()
    with ProviderSession(cfg) as session:
        provider = session.provider_factory("anthropic")
        assert isinstance(provider, Provider)
        opened = _retrying_clients(seam["clients"])
        assert opened, "the anthropic builder must open a retrying client"
        assert not any(c.is_closed for c in opened), "client closed before the session exited"

    # Leaving the session closes every client its builders opened.
    assert all(c.is_closed for c in opened), "the session did not close what its builders opened"


def test_openai_responses_is_resolvable_without_a_rebar_builder():
    """The ``openai-responses`` provider must resolve so the ticket-155c default-flipped
    ``openai-responses:<model>`` string is a live target, not a config error.

    This pins the SOLE purpose of the ``_EXTRA_KNOWN_PROVIDERS`` union in providers.py: rebar
    registers NO builder for ``openai-responses`` (its OpenAI builder answers only under
    ``openai``/``openai-chat``, and only when a ``base_url`` is set), so with no custom endpoint
    the name is resolvable purely because it is folded into ``_pydantic_ai_known_providers`` —
    handed to pydantic-ai's own resolution exactly like ``openai-chat``. Checked WITHOUT
    constructing anything (no OpenAI credentials required)."""
    from rebar.llm.providers import ProviderSession

    cfg = _cfg()  # no base_url → the OpenAI builder is NOT registered
    with ProviderSession(cfg) as session:
        assert session.is_resolvable("openai-responses") is True
        assert session.supports("openai-responses") is False, (
            "rebar must register no builder for openai-responses; it resolves via pydantic-ai"
        )
        # Hosted Chat fallback is removed; custom endpoints still register the Chat builder.
        assert session.is_resolvable("openai-chat") is False
        assert session.supports("openai-chat") is False


def test_hosted_openai_chat_provider_factory_is_removed():
    from rebar.llm.providers import ProviderSession

    cfg = _cfg()  # no base_url → hosted OpenAI
    with ProviderSession(cfg) as session:
        with pytest.raises(LLMConfigError) as excinfo:
            session.provider_factory("openai-chat")

    message = str(excinfo.value)
    assert "openai-chat" in message
    assert "openai-responses" in message
    assert "base_url" in message or "endpoint" in message


def test_provider_session_factory_rejects_an_unknown_provider_by_name():
    """An unregistered name is a typed rebar config error naming what IS registered — the
    registry lookup must not surface a bare ``KeyError``."""
    from rebar.llm.providers import ProviderSession

    cfg = _cfg()
    with ProviderSession(cfg) as session:
        with pytest.raises(LLMConfigError) as excinfo:
            session.provider_factory("notaprovider")

    message = str(excinfo.value)
    assert "notaprovider" in message
    assert "anthropic" in message, "the error must name the registered providers"


# ── Held out from the implementer ───────────────────────────────────────────────────────


def test_session_closes_the_client_when_the_run_raises(seam):
    """The close is owned by the session's exit, not by the success path.

    A 400 is NOT in the transport's retry set, so it surfaces as a run failure — and the client
    must still be closed. An implementation that closes only after a successful return passes the
    success test above and fails here, which is exactly why this case is withheld."""
    seam["status"]["code"] = 400
    cfg = _cfg()

    with pytest.raises(Exception):  # noqa: B017 — any failure; the oracle is the closure below
        PydanticAIRunner(cfg).run(_req(cfg))

    built = _retrying_clients(seam["clients"])
    assert len(built) == 1, "the seam did not build its client before failing"
    assert built[0].is_closed is True, "the seam-built client leaked when the run raised"


def test_unregistered_provider_raises_llm_config_error_naming_registered_providers(seam):
    """An unknown provider is a rebar CONFIG error naming what rebar can build — not a raw
    ``KeyError`` from a dict lookup and not an opaque upstream error."""
    cfg = _cfg(model="notaprovider:some-model")

    with pytest.raises(LLMConfigError) as excinfo:
        PydanticAIRunner(cfg).run(_req(cfg))

    message = str(excinfo.value)
    assert "notaprovider" in message
    assert "anthropic" in message, "the error must name the registered providers"


def test_model_override_path_builds_no_client_and_closes_nothing(seam):
    """Negative control: the injected-model path must not touch the seam at all.

    This is the input where behavior must NOT change — it proves the closure assertions above are
    detecting real construction rather than passing vacuously."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def _gen(messages, info):
        return ModelResponse(parts=[TextPart("done")])

    cfg = _cfg()
    PydanticAIRunner(cfg, model_override=FunctionModel(_gen)).run(_req(cfg))

    assert _retrying_clients(seam["clients"]) == [], (
        "the model_override path must build no provider client"
    )
