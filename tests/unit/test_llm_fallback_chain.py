"""Per-class fallback chains: `FallbackModel` construction and selection (task cc33).

A class may name a model that is not always reachable — a local endpoint that is down, a
provider being throttled. These pin the chain rebar builds from a class slot's `fallback`
array, the condition under which it fails over, and the four facts a chain silently gets
wrong if it is built naively: the per-entry `endpoint`, the SHARED client lifecycle, the
CONSERVATIVE capability intersection, and WHICH model a verdict attests.

Everything is real except the socket: the production Anthropic builder runs against a
`MockTransport` that answers per requested model id, so failover is exercised through the
real `FallbackModel.request` loop rather than a call-count spy. Assertions land on returned
values, constructed object state, and what the run attests — never on private structure.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest

from rebar.llm import structured_run as structured_run_mod

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
import pydantic_ai.models.fallback
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.retries import AsyncTenacityTransport

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_PRIMARY = "claude-sonnet-4-6"
# MEASURED to reject `temperature` (capabilities.py's exact-id override) — the discriminating
# fallback entry for the whole-chain capability test.
_NO_TEMPERATURE = "claude-opus-4-8"

#: The endpoint a fallback entry is configured against. Single-sourced so the configured value and
#: the asserted value cannot drift.
_FALLBACK_ENDPOINT = "https://fallback.test"


def _anthropic_expects_httpx2_client() -> bool:
    import inspect

    import anthropic

    http_client = inspect.signature(anthropic.AsyncAnthropic.__init__).parameters.get("http_client")
    return http_client is not None and "httpx2.AsyncClient" in str(http_client.annotation)


def _transport_http_module():
    if _anthropic_expects_httpx2_client():
        return pytest.importorskip("httpx2")
    return httpx


def _origin(url: object) -> tuple[str, str]:
    """The ``(scheme, host)`` pair of ``url``, for comparing endpoints by ORIGIN.

    A URL must never be checked with ``startswith`` or ``in``: ``https://fallback.test.evil.example``
    starts with ``https://fallback.test``, so a prefix test would accept an entirely different host
    (CodeQL ``py/incomplete-url-substring-sanitization``). Comparing the parsed origin by exact
    equality has no substring semantics at all, while still tolerating the trailing-slash and path
    normalisation a client applies to a configured endpoint — which is what the prefix test was
    reaching for."""
    parts = urlsplit(str(url))
    return parts.scheme, parts.netloc


def _mc():
    """Import at call time so a missing symbol fails the TEST, not collection of the file."""
    from rebar.llm import model_classes

    return model_classes


def _ok_body(model: str) -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _err_body(message: str = "nope") -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


@pytest.fixture(autouse=True)
def _anthropic_env(monkeypatch):
    """The real `AsyncAnthropic` builds auth headers before the MockTransport is reached, so it
    needs *a* key present. `ANTHROPIC_BASE_URL` is cleared so a dev machine's ambient loopback
    value does not enter the proxy-bypass branch, and the class env overrides are cleared so an
    operator's own `REBAR_LLM_*` settings cannot rewrite the slots under test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in ("FRONTIER", "STANDARD", "TRIVIAL"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{name}_{field}", raising=False)


@pytest.fixture
def seam(monkeypatch):
    """Real construction, mocked socket, with per-model control of the answer.

    - `status`: `{model_id: http_status}`, defaulting to 200 — how a specific candidate answers.
    - `seen`: the model ids the transport was actually asked for, in order, so "the fallback was
      never reached" is observable rather than inferred.
    - `clients`: every `httpx.AsyncClient` built, each counting its own `aclose()` calls, so
      "closed exactly once" is asserted on the real object.
    - `captured`: what `run()` handed its `Agent`, plus an event log ordering model entry,
      agent construction and model exit.
    """
    transport_http = _transport_http_module()
    clients: list = []
    real_client_cls = transport_http.AsyncClient
    status: dict[str, int] = {}
    seen: list[str] = []
    events: list[str] = []

    class _CountingAsyncClient(real_client_cls):  # type: ignore[valid-type,misc]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.aclose_calls = 0
            clients.append(self)

        async def aclose(self, *a, **kw):
            self.aclose_calls += 1
            return await super().aclose(*a, **kw)

    def _handler(request):
        name = json.loads(request.content).get("model", "")
        seen.append(name)
        code = status.get(name, 200)
        if code == 200:
            return transport_http.Response(200, json=_ok_body(name))
        return transport_http.Response(code, json=_err_body())

    monkeypatch.setattr(transport_http, "AsyncClient", _CountingAsyncClient)
    monkeypatch.setattr(
        transport_http,
        "AsyncHTTPTransport",
        lambda *a, **kw: transport_http.MockTransport(_handler),
    )

    captured: dict = {}
    real_import = structured_run_mod._import_pydantic_ai

    def _capturing_import():
        real_agent = real_import()

        def _agent(model, **kw):
            captured["model"] = model
            captured["kwargs"] = kw
            events.append("agent")
            return real_agent(model, **kw)

        return _agent

    monkeypatch.setattr(structured_run_mod, "_import_pydantic_ai", _capturing_import)

    class _RecordingFallbackModel(FallbackModel):
        """Records the async-context-manager protocol the runner must drive, without changing
        it — `super()` still does the real sub-model entry/exit."""

        async def __aenter__(self):
            events.append("enter")
            return await super().__aenter__()

        async def __aexit__(self, *args):
            events.append("exit")
            return await super().__aexit__(*args)

    monkeypatch.setattr(pydantic_ai.models.fallback, "FallbackModel", _RecordingFallbackModel)
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)

    return {
        "clients": clients,
        "captured": captured,
        "status": status,
        "seen": seen,
        "events": events,
    }


def _retrying_clients(clients) -> list:
    """The clients the provider seam built — identified by the retrying transport the builder
    installs, so an unrelated client another library opens cannot inflate the count."""
    return [
        c
        for c in clients
        if isinstance(getattr(c, "_transport", None), AsyncTenacityTransport)
        or getattr(c, "_transport", None).__class__.__name__ == "_Httpx2AsyncTenacityTransport"
    ]


def _configure_chain(monkeypatch, fallback: list[dict], *, primary: str = _PRIMARY) -> None:
    """Point the `standard` class slot at `primary` with `fallback` as its ordered chain, through
    the REAL config path (`load_class_slots` -> `_read_llm_file_table`) rather than by patching
    the resolver — so the chain has to survive parsing to reach the runner."""
    from rebar.llm import config as llm_config

    table = {
        "model_classes": {
            "standard": {"model": primary, "provider": "anthropic", "fallback": fallback}
        }
    }
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: table)


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    kw.setdefault("model", f"anthropic:{_PRIMARY}")
    # One attempt: the transport's own tenacity retry would otherwise re-send a 529 to the
    # PRIMARY several times before the chain is ever consulted — the SDK-retry delay the
    # `fallback_on` docstring warns about, and noise in a failover test.
    kw.setdefault("llm_retry_max_attempts", 1)
    return LLMConfig(**kw)


def _req(cfg) -> RunRequest:
    # single_turn => tools=[] and toolsets=[], so the run needs no gate session and makes
    # exactly one model call through the chain under test.
    return RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        mode="text",
        execution_mode="single_turn",
    )


# ── construction: with and without a chain ────────────────────────────────────────────────


def test_a_class_without_fallback_builds_an_unwrapped_model(seam, monkeypatch):
    """The rollback contract: an operator who configures no `fallback` must get exactly today's
    model object. A chain wrapper applied unconditionally would change the error surface (every
    failure becomes a `FallbackExceptionGroup`) for every existing deployment."""
    _configure_chain(monkeypatch, [])
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    model = seam["captured"]["model"]
    assert not isinstance(model, FallbackModel)
    assert type(model).__name__ == "AnthropicModel"


def test_a_class_with_fallback_builds_a_chain_whose_default_is_the_primary(seam, monkeypatch):
    """Order is load-bearing: the primary must be tried FIRST, with the configured entries after
    it in declaration order. A chain that reordered them would silently spend on the wrong tier."""
    _configure_chain(
        monkeypatch,
        [
            {"model": _NO_TEMPERATURE, "provider": "anthropic"},
            {"model": "claude-haiku-4-5", "provider": "anthropic"},
        ],
    )
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    model = seam["captured"]["model"]
    assert isinstance(model, FallbackModel)
    assert [m.model_name for m in model.models] == [_PRIMARY, _NO_TEMPERATURE, "claude-haiku-4-5"]


def test_a_fallback_entry_endpoint_is_built_against_that_endpoint(seam, monkeypatch):
    """`resolve_fallback_chain` returns `provider:model` STRINGS, which cannot carry an endpoint —
    so a chain built from those strings silently sends every entry to the provider default. That
    would defeat the main reason to configure a fallback: a local proxy backing a hosted primary."""
    _configure_chain(
        monkeypatch,
        [{"model": _NO_TEMPERATURE, "provider": "anthropic", "endpoint": _FALLBACK_ENDPOINT}],
    )
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    model = seam["captured"]["model"]
    assert _origin(model.models[1].base_url) == _origin(_FALLBACK_ENDPOINT)
    # The PRIMARY entry must still go to the provider default, not the fallback's endpoint.
    assert _origin(model.models[0].base_url) != _origin(_FALLBACK_ENDPOINT)


def test_a_host_that_merely_shares_the_endpoint_prefix_is_not_the_same_origin():
    """The comparison must reject a look-alike host, which is what a prefix test could not.

    `https://fallback.test.evil.example` STARTS WITH `https://fallback.test`, so the assertion this
    file used to make would have accepted it as "built against that endpoint". Pinned here so the
    weak form cannot come back: an origin comparison rejects it, and the prefix test it replaced
    would have accepted it."""
    look_alike = "https://fallback.test.evil.example/v1"
    assert _origin(look_alike) != _origin(_FALLBACK_ENDPOINT)
    # ...and this is exactly what the replaced prefix check would have got wrong:
    assert look_alike.startswith(_FALLBACK_ENDPOINT)

    # Normalisation the client may apply must still compare EQUAL — the tolerance the prefix
    # check was reaching for, kept without the weakness.
    assert _origin(_FALLBACK_ENDPOINT + "/") == _origin(_FALLBACK_ENDPOINT)
    assert _origin(_FALLBACK_ENDPOINT + "/v1/messages") == _origin(_FALLBACK_ENDPOINT)


# ── selection: what does and does not trigger failover ────────────────────────────────────


def test_an_error_in_the_condition_selects_the_fallback(seam, monkeypatch):
    """529 (provider overloaded) is `WAIT_AND_RETRY` — switching providers genuinely helps, so the
    next candidate must answer and the run must succeed."""
    _configure_chain(monkeypatch, [{"model": _NO_TEMPERATURE, "provider": "anthropic"}])
    seam["status"][_PRIMARY] = 529
    cfg = _cfg()

    result = PydanticAIRunner(cfg).run(_req(cfg))

    assert seam["seen"] == [_PRIMARY, _NO_TEMPERATURE], "the chain did not fail over in order"
    assert result["provider_provenance"]["ran_model"].endswith(_NO_TEMPERATURE)


def test_an_error_outside_the_condition_propagates_without_failing_over(seam, monkeypatch):
    """A 401 is a CREDENTIAL problem (`CHANGE_SETTINGS`). Shopping for a provider whose key happens
    to work masks it, so the error must propagate and the fallback must never be called."""
    _configure_chain(monkeypatch, [{"model": _NO_TEMPERATURE, "provider": "anthropic"}])
    seam["status"][_PRIMARY] = 401
    cfg = _cfg()

    with pytest.raises(LLMUnavailableError):
        PydanticAIRunner(cfg).run(_req(cfg))

    assert seam["seen"] == [_PRIMARY], "a credential failure must not be relocated to a fallback"


# ── lifecycle: one session owns every client, and the wrapper is entered ───────────────────


def test_every_chain_client_is_closed_exactly_once_by_the_one_session(seam, monkeypatch):
    """N Anthropic-typed fallbacks plus the primary means N+1 rebar-created transports, and they
    must all ride the SAME `ProviderSession`: a session per entry closes only its own client and
    leaks the rest. `aclose` is counted on the real client, so a double-close (registering
    pydantic-ai's own clients too) is caught alongside the leak."""
    _configure_chain(
        monkeypatch,
        [
            {"model": _NO_TEMPERATURE, "provider": "anthropic"},
            {"model": "claude-haiku-4-5", "provider": "anthropic"},
        ],
    )
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    built = _retrying_clients(seam["clients"])
    assert len(built) == 3, f"expected primary + 2 fallback clients, got {len(built)}"
    assert all(c.is_closed for c in built), "a chain client leaked after the run"
    assert [c.aclose_calls for c in built] == [1, 1, 1], "a chain client was closed twice"


def test_the_chain_is_driven_as_an_async_context_manager(seam, monkeypatch):
    """`agent.run_sync` does NOT enter the model (only `async with agent` does), so unless rebar
    enters the wrapper the sub-models' providers are never entered — the exact thing
    `FallbackModel.__aenter__` exists to do ("so their providers can manage HTTP client
    lifecycle"). Entry must bracket the run, not follow it."""
    _configure_chain(monkeypatch, [{"model": _NO_TEMPERATURE, "provider": "anthropic"}])
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    assert seam["events"] == ["enter", "agent", "exit"]


# ── capabilities and provenance over the CANDIDATE SET ─────────────────────────────────────


def test_a_chain_withdraws_temperature_for_the_whole_chain(seam, monkeypatch):
    """`claude-opus-4-8` is MEASURED to reject `temperature`. Computing capabilities from the
    PRIMARY alone leaves `temperature` in the request, so the run then succeeds or 400s depending
    on which candidate answered — a failure reproducing only under provider degradation."""
    _configure_chain(monkeypatch, [{"model": _NO_TEMPERATURE, "provider": "anthropic"}])
    cfg = _cfg(temperature=0.0)
    PydanticAIRunner(cfg).run(_req(cfg))

    settings = seam["captured"]["kwargs"].get("model_settings") or {}
    assert "temperature" not in settings, "a candidate that rejects temperature did not withdraw it"


def test_a_chain_without_a_restricted_candidate_still_sends_temperature(seam, monkeypatch):
    """The negative control for the intersection: it must WITHDRAW on the restricted candidate,
    not withdraw wholesale the moment a chain exists."""
    _configure_chain(monkeypatch, [{"model": "claude-haiku-4-5", "provider": "anthropic"}])
    cfg = _cfg(temperature=0.0)
    PydanticAIRunner(cfg).run(_req(cfg))

    assert isinstance(seam["captured"]["model"], FallbackModel), "no chain was built to control"
    settings = seam["captured"]["kwargs"].get("model_settings") or {}
    assert settings.get("temperature") == 0.0


def test_capabilities_are_never_resolved_from_the_wrapper(seam, monkeypatch):
    """`FallbackModel` has no profile of its own and its `.provider` is None, so
    `capabilities_for(wrapper)` returns DEFAULT capabilities silently — no error, just the wrong
    answer. It must be called per candidate instead."""
    import rebar.llm.runner as runner_mod

    real = runner_mod.capabilities_for
    args: list = []

    def _recording(arg):
        args.append(arg)
        return real(arg)

    monkeypatch.setattr(runner_mod, "capabilities_for", _recording)
    _configure_chain(monkeypatch, [{"model": _NO_TEMPERATURE, "provider": "anthropic"}])
    cfg = _cfg()
    PydanticAIRunner(cfg).run(_req(cfg))

    assert isinstance(seam["captured"]["model"], FallbackModel), "no chain was built to control"
    assert not any(isinstance(a, FallbackModel) for a in args)
    resolved = {getattr(a, "model_name", a) for a in args}
    assert {_PRIMARY, _NO_TEMPERATURE} <= resolved, "a candidate's capabilities were never read"


def test_provenance_records_the_ordered_candidates_and_the_model_that_answered(seam, monkeypatch):
    """`FallbackModel.model_name` is the synthetic `fallback:a,b` string and is useless for
    attestation. A verdict produced by a fallback must name THAT model, not the primary — the
    misattribution the provider-provenance work exists to prevent."""
    _configure_chain(
        monkeypatch,
        [
            {"model": _NO_TEMPERATURE, "provider": "anthropic"},
            {"model": "claude-haiku-4-5", "provider": "anthropic"},
        ],
    )
    seam["status"][_PRIMARY] = 529
    cfg = _cfg()

    result = PydanticAIRunner(cfg).run(_req(cfg))

    provenance = result["provider_provenance"]
    assert provenance["candidates"] == [
        f"anthropic:{_PRIMARY}",
        f"anthropic:{_NO_TEMPERATURE}",
        "anthropic:claude-haiku-4-5",
    ]
    ran = provenance["ran_model"]
    assert not ran.startswith("fallback:"), "the synthetic wrapper name was attested"
    assert ran.endswith(_NO_TEMPERATURE)
    assert not ran.endswith(_PRIMARY)


# ── the `fallback_on` predicate ────────────────────────────────────────────────────────────


def _http_error(status: int, message: str = "boom"):
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(status_code=status, model_name="m", body={"error": {"message": message}})


def test_the_predicate_fires_on_availability_failures_only():
    """The table the chain is FOR: a down endpoint and a throttled/overloaded provider fail over;
    a credential problem and an oversized request fail LOUDLY instead."""
    mc = _mc()
    assert mc.should_fall_back(httpx.ConnectError("down")) is True
    assert mc.should_fall_back(httpx.ConnectTimeout("down")) is True
    assert mc.should_fall_back(_http_error(529)) is True
    assert mc.should_fall_back(_http_error(503)) is True
    assert mc.should_fall_back(_http_error(401)) is False
    assert mc.should_fall_back(_http_error(403)) is False
    assert mc.should_fall_back(_http_error(400, "prompt is too long")) is False


def test_the_predicate_splits_the_two_kinds_of_429():
    """A plain rate limit is transient and relocatable; a 429 carrying `insufficient_quota` is a
    SPEND problem, and silently moving spend to another provider is worse than failing loudly."""
    mc = _mc()
    assert mc.should_fall_back(_http_error(429, "rate limit exceeded")) is True
    assert mc.should_fall_back(_http_error(429, "insufficient_quota")) is False


def test_the_predicate_is_derived_from_the_resolution_taxonomy():
    """Not a hand-maintained tuple of exception types: the answer must come from
    `classify_llm_failure`, so a taxonomy change reaches the chain automatically. Driven through
    the classifier over every class, with a sentinel exception no type-tuple could match."""
    from rebar.llm import failure

    mc = _mc()
    sentinel = RuntimeError("sentinel")
    expected = {
        failure.ResolutionClass.RETRY_NOW: True,
        failure.ResolutionClass.WAIT_AND_RETRY: True,
        failure.ResolutionClass.CHANGE_PROVIDER_OR_MODEL: True,
        failure.ResolutionClass.CHANGE_SETTINGS: False,
        failure.ResolutionClass.CHANGE_INPUT: False,
        failure.ResolutionClass.INCREASE_PROVIDER_LIMITS: False,
        failure.ResolutionClass.FIX_AGENT_DESIGN: False,
        failure.ResolutionClass.NEEDS_INVESTIGATION: False,
    }
    for resolution_class, should in expected.items():
        outcome = failure.LLMOutcome(
            resolution_class=resolution_class, diagnostic={}, retryable=False
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(failure, "classify_llm_failure", lambda *a, _o=outcome, **kw: _o)
            assert mc.should_fall_back(sentinel) is should, resolution_class


def test_the_predicate_tracks_the_retryable_frozenset(monkeypatch):
    """The no-drift claim, made falsifiable: widening `_RETRYABLE` must widen failover WITHOUT
    editing this feature. A predicate carrying its own copy of the retryable set passes the
    table above and fails here."""
    from rebar.llm import failure

    mc = _mc()
    context_error = _http_error(400, "prompt is too long")
    assert mc.should_fall_back(context_error) is False

    monkeypatch.setattr(
        failure, "_RETRYABLE", failure._RETRYABLE | {failure.ResolutionClass.CHANGE_INPUT}
    )
    assert mc.should_fall_back(context_error) is True
