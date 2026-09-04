"""Provider/model resolution + Anthropic model construction (leaf).

The provider-agnostic runner (``runner.PydanticAIRunner``) picks a Pydantic AI
model purely from a provider-qualified model string. This module holds that
resolution cluster (``_pai_model`` + the ``_PAI_PROVIDER_PREFIX`` map) together
with the Anthropic-specific construction path the runner funnels through on the
``anthropic:…`` provider: the retrying transport client
(``_build_retrying_anthropic_model``) and the loopback-proxy bypass
(``_local_proxy_bypass_base_url``). Prompt-cache settings live in
``rebar.llm.capabilities`` (story S2) — that module reads capability FIELDS off a
``ModelProfile`` rather than string-matching a provider name, which this module's old
the removed anthropic-only cache-settings helper (``startswith`` gated) did not: it
silently disabled caching for Bedrock-hosted Claude. The web-search capability helper
(once ``_anthropic_web_search_capabilities`` here, ``startswith``-gated in the same way)
moved to that module too and stopped being provider-gated at all — bug 129e: the Bedrock
cutover silently withdrew the T1 blocking criterion's grounding tool.

Heavy libraries (httpx, anthropic, pydantic-ai, tenacity, urllib) are imported
**inside** the functions that need them, never at module top, so this module
keeps the stdlib-only ``import rebar.llm`` contract that ``runner`` relies on.
This is a leaf: it imports nothing back from ``runner``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)


# Internal provider names -> the Pydantic AI model-string prefix. A small, declarative
# map (NOT per-provider behaviour) so the provider is chosen purely by the model string.
# NOTE (ticket 155c): the bare ``openai`` request is resolved BEFORE this map, by
# ``_openai_wire_prefix`` (Responses by default, Chat for a custom ``base_url``), so there is
# deliberately no ``"openai"`` row here — a static ``openai:*`` prefix cannot encode that
# base_url-dependent choice.
_PAI_PROVIDER_PREFIX = {
    "anthropic": "anthropic",
    "google_genai": "google-gla",
    "google": "google-gla",
}


_DIRECT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def _local_proxy_bypass_base_url() -> str | None:
    """The DIRECT Anthropic base_url to use INSTEAD of a loopback ``ANTHROPIC_BASE_URL``,
    or ``None`` when no bypass applies.

    A local Claude-Code payload optimizer (e.g. headroom on ``127.0.0.1``) inherited via
    ``ANTHROPIC_BASE_URL`` corrupts rebar's own multi-turn agentic tool-loop requests into
    an empty provider stream (bug sue-skimp-tear), so rebar's internal agent must talk to
    Anthropic directly. Returns the direct public API URL ONLY when ``ANTHROPIC_BASE_URL``
    is set to a loopback host; a non-loopback gateway is respected (``None``), an unset var
    is a no-op (``None``), and ``REBAR_LLM_ALLOW_LOCAL_PROXY`` truthy opts back in
    (``None``)."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return None
    if os.environ.get("REBAR_LLM_ALLOW_LOCAL_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None
    from urllib.parse import urlparse

    host = (urlparse(base).hostname or "").strip().lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return _DIRECT_ANTHROPIC_BASE_URL
    return None


# HTTP statuses the transport retries. 529 (Anthropic overloaded) is included explicitly —
# pydantic-ai's sample retry list omits it. Status codes are not exceptions by default, so
# `validate_response` raises for them (below) to make the retry predicate fire.
_RETRY_STATUSES = frozenset({429, 529, 500, 502, 503, 504})


def _retry_after_delay(exc: BaseException, *, max_wait: float) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = headers.get("retry-after") if headers is not None else None
    if not retry_after:
        return None
    try:
        return min(float(int(retry_after)), max_wait)
    except ValueError:
        pass
    try:
        retry_time = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return None
    if retry_time.tzinfo is None:
        retry_time = retry_time.replace(tzinfo=timezone.utc)
    wait_seconds = (retry_time - datetime.now(timezone.utc)).total_seconds()
    if wait_seconds <= 0:
        return None
    return min(wait_seconds, max_wait)


def _anthropic_http_client_module(async_anthropic_cls):
    import inspect

    import httpx

    http_client = inspect.signature(async_anthropic_cls.__init__).parameters.get("http_client")
    if http_client is not None and "httpx2.AsyncClient" in str(http_client.annotation):
        import httpx2

        return httpx2
    return httpx


def _coerce_timeout_for_http_client(http_module, *, cfg: LLMConfig, http_timeout):
    if http_timeout is None:
        return http_module.Timeout(float(cfg.timeout_s))
    if http_module.__name__ != "httpx2" or isinstance(http_timeout, http_module.Timeout):
        return http_timeout
    return http_module.Timeout(
        connect=getattr(http_timeout, "connect", None),
        read=getattr(http_timeout, "read", None),
        write=getattr(http_timeout, "write", None),
        pool=getattr(http_timeout, "pool", None),
    )


def _build_httpx2_tenacity_transport(httpx2, *, config, wrapped=None, validate_response=None):
    import pydantic_ai.retries as pai_retries

    HTTPX2TenacityTransport = getattr(pai_retries, "HTTPX2TenacityTransport", None)

    if HTTPX2TenacityTransport is not None:
        return HTTPX2TenacityTransport(
            config=config,
            wrapped=wrapped or httpx2.AsyncHTTPTransport(),
            validate_response=validate_response,
        )

    class _Httpx2AsyncTenacityTransport(httpx2.AsyncBaseTransport):  # type: ignore[name-defined]
        def __init__(self):
            self.config = config
            self.wrapped = wrapped or httpx2.AsyncHTTPTransport()
            self.validate_response = validate_response

        async def handle_async_request(self, request):
            from tenacity import retry

            @retry(**self.config)
            async def _handle(req):
                response = await self.wrapped.handle_async_request(req)
                response.request = req
                if self.validate_response is not None:
                    try:
                        self.validate_response(response)
                    except Exception:
                        await response.aclose()
                        raise
                return response

            return await _handle(request)

        async def __aenter__(self):
            await self.wrapped.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            await self.wrapped.__aexit__(exc_type, exc_value, traceback)

        async def aclose(self) -> None:
            await self.wrapped.aclose()

    return _Httpx2AsyncTenacityTransport()


def _is_anthropic_omit(value: object) -> bool:
    return value.__class__.__name__ == "Omit" and value.__class__.__module__.startswith("anthropic")


def _move_unsupported_beta_create_kwargs(anthropic_client: Any) -> None:
    import inspect

    create = getattr(
        getattr(getattr(anthropic_client, "beta", None), "messages", None), "create", None
    )
    if create is None:
        return
    params = inspect.signature(create).parameters
    unsupported = tuple(name for name in ("temperature", "top_p", "top_k") if name not in params)
    if not unsupported:
        return

    async def _create(*args, **kwargs):
        extra_body = kwargs.get("extra_body")
        if extra_body is not None:
            extra_body = dict(extra_body)
        for name in unsupported:
            value = kwargs.pop(name, None)
            if value is not None and not _is_anthropic_omit(value):
                if extra_body is None:
                    extra_body = {}
                extra_body[name] = value
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        return await create(*args, **kwargs)

    anthropic_client.beta.messages.create = _create


def _build_retry_wait(*, max_wait: float, rng=None):
    """Build the tenacity wait strategy for the Anthropic transport (ticket
    ``254e-1770-854b-47a2``). Hardens pydantic-ai's ``wait_retry_after`` against two
    thundering-herd vectors that surface when many agent processes share ONE throttled
    account (incident 1c0d):

    1. **Jittered fallback.** With no ``Retry-After``, pydantic-ai's default fallback is a
       NON-jittered ``wait_exponential``, so co-throttled clients back off by the identical
       amount and retry in lockstep. Replace it with Equal Jitter ``rng.uniform(cap / 2, cap)``
       over an exponential cap (Marc Brooker's AWS taxonomy; the gobifrost/bifrost pattern) so
       concurrent clients scatter.
    2. **Zero/expired ``Retry-After`` guard.** A ``Retry-After: 0`` (or negative) integer can
       otherwise collapse to an IMMEDIATE replay — re-forming the herd.
       Treat a non-positive integer ``Retry-After`` as ABSENT and fall through to the jittered
       fallback. (An already-expired HTTP-date falls through the same way.)

    A positive in-window ``Retry-After`` is still honored (capped at ``max_wait``). ``rng``
    defaults to the process-global ``random`` module, which
    Python seeds from OS entropy once per interpreter start — so the N SEPARATE agent processes
    that form the herd draw from independent streams; it is an injection seam for deterministic
    tests only, never threaded from production config.
    """
    import random as _random

    _rng = rng if rng is not None else _random

    def _jittered_fallback(state) -> float:
        cap = min(2.0 ** (state.attempt_number - 1), max_wait)
        if cap <= 0:
            return 0.0
        return _rng.uniform(cap / 2.0, cap)

    def _wait(state) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if exc is not None:
            retry_after_delay = _retry_after_delay(exc, max_wait=max_wait)
            if retry_after_delay is not None and retry_after_delay > 0:
                return retry_after_delay
        return _jittered_fallback(state)

    return _wait


def _build_retrying_anthropic_provider(
    *, base_url: str | None, cfg: LLMConfig, http_timeout=None, _wrapped_transport=None, auth=None
):
    """Build the ``AnthropicProvider`` wrapping an ``AsyncAnthropic`` client that carries a
    retrying ``AsyncTenacityTransport`` (story morbid-uncultured-arcticduck). Retry is owned
    SOLELY by the transport (SDK ``max_retries=0``); a construction-time guard fails fast rather
    than silently regress to SDK-managed retries. Returns ``(provider, http_client)`` — the
    caller closes ``http_client`` on run teardown via ``asyncio.run(http_client.aclose())``.

    This is the entry point ``ProviderSession._build_anthropic`` uses: the ``provider_factory``
    hook's contract is a ``Provider`` (not a ``Model``), so building the provider directly avoids
    constructing a throwaway ``AnthropicModel`` just to read ``.provider`` off it.

    ``base_url=None`` uses the Anthropic SDK default (the normal path); a non-empty value is
    the loopback-proxy-bypass direct URL. ``http_timeout`` is story hoopoe's per-attempt
    ``httpx.Timeout`` when present (coerced to ``httpx2.Timeout`` for the SDKs that require
    it), else a bounded default from ``cfg.timeout_s`` (never unbounded). A transient
    ``{429,529,5xx}``/timeout/network blip is re-sent BELOW the agent loop, so completed tool
    calls are never re-executed; a positive ``Retry-After`` is honored (capped at
    ``llm_retry_max_wait_s``), else a jittered exponential backoff (``_build_retry_wait``; a
    zero/negative/expired ``Retry-After`` is guarded to that jittered fallback rather than an
    immediate replay — ticket 254e).

    ``auth`` is the optional RP-04 S4 :class:`~rebar.llm.auth.AnthropicAuth` carrier. When
    SUPPLIED it is fail-closed-validated (exactly one of ``api_key``/``auth_token`` — a
    conflicting or empty carrier raises :class:`LLMConfigError` BEFORE the client is built,
    never degrading to the ambient credential) and its single key is injected into the
    ``AsyncAnthropic(...)`` call; ``None`` means the SDK resolves its ambient credential
    exactly as before RP-04."""
    from anthropic import AsyncAnthropic
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig
    from tenacity import retry_if_exception_type, stop_after_attempt

    # Fail-closed BEFORE building the client so an invalid carrier never falls back to ambient.
    auth_kwargs: dict[str, Any] = {}
    if auth is not None:
        from rebar.llm.auth import anthropic_auth_kwargs

        auth_kwargs = anthropic_auth_kwargs(auth)

    http_module = _anthropic_http_client_module(AsyncAnthropic)

    def _validate_response(response: Any) -> None:
        if response.status_code in _RETRY_STATUSES:
            response.raise_for_status()

    def _before_sleep(state) -> None:
        sleep = getattr(getattr(state, "next_action", None), "sleep", None)
        logger.warning(
            "llm transport retry: attempt %d failed, sleeping %.1fs before retry",
            state.attempt_number,
            float(sleep or 0.0),
        )

    attempts = max(1, int(cfg.llm_retry_max_attempts))
    retry_config = RetryConfig(
        retry=(
            retry_if_exception_type(http_module.HTTPStatusError)
            | retry_if_exception_type(http_module.TimeoutException)
            | retry_if_exception_type(http_module.NetworkError)
        ),
        wait=_build_retry_wait(max_wait=float(cfg.llm_retry_max_wait_s)),
        stop=stop_after_attempt(attempts),
        reraise=True,
        before_sleep=_before_sleep,
    )
    wrapped = (
        _wrapped_transport if _wrapped_transport is not None else http_module.AsyncHTTPTransport()
    )
    if http_module.__name__ == "httpx2":
        transport = _build_httpx2_tenacity_transport(
            http_module, config=retry_config, wrapped=wrapped, validate_response=_validate_response
        )
    else:
        transport = AsyncTenacityTransport(
            config=retry_config,
            wrapped=wrapped,
            validate_response=_validate_response,
        )
    timeout = _coerce_timeout_for_http_client(http_module, cfg=cfg, http_timeout=http_timeout)
    http_client = http_module.AsyncClient(transport=transport, timeout=timeout)
    anthropic_client = AsyncAnthropic(
        base_url=base_url or None, max_retries=0, http_client=http_client, **auth_kwargs
    )
    # Construction-time guard: never silently regress to SDK-managed retries.
    if anthropic_client.max_retries != 0:
        raise LLMConfigError(
            "transport-retry guard: AsyncAnthropic.max_retries must be 0 "
            "(retry is owned by the httpx transport, not the SDK)"
        )
    _move_unsupported_beta_create_kwargs(anthropic_client)
    return AnthropicProvider(anthropic_client=anthropic_client), http_client


def _build_retrying_anthropic_model(
    name: str,
    *,
    base_url: str | None,
    cfg: LLMConfig,
    http_timeout=None,
    _wrapped_transport=None,
    auth=None,
):
    """Build an ``AnthropicModel`` on the retrying provider from
    :func:`_build_retrying_anthropic_provider`. Returns ``(model, http_client)`` — the caller
    closes ``http_client`` on run teardown via ``asyncio.run(http_client.aclose())``. See that
    function for the transport/retry/timeout and ``auth`` (RP-04 S4) semantics."""
    from pydantic_ai.models.anthropic import AnthropicModel

    provider, http_client = _build_retrying_anthropic_provider(
        base_url=base_url,
        cfg=cfg,
        http_timeout=http_timeout,
        _wrapped_transport=_wrapped_transport,
        auth=auth,
    )
    model = AnthropicModel(name, provider=provider)
    return model, http_client


def _pai_model(cfg: LLMConfig):
    """The Pydantic AI model string for ``cfg`` (provider-qualified). If ``cfg.model``
    already carries a ``provider:`` prefix it is preserved, except that an OpenAI-family
    request (``openai:`` / ``openai-chat:`` / ``model_provider`` set to either) is made
    explicit: ``openai-responses:`` for hosted OpenAI, or ``openai-chat:`` when a custom
    ``base_url`` is configured (a custom OpenAI-compatible endpoint stays on Chat
    Completions — rebar builds it only under the ``openai``/``openai-chat`` keys and vendor
    ``/v1/responses`` support is UNKNOWN). Otherwise the provider is inferred
    (or taken from ``cfg.model_provider``) and mapped to Pydantic AI's prefix — no per-provider
    construction code, the string is the only switch."""
    m = cfg.model
    if ":" in m:
        if m.startswith("openai-chat:") and not cfg.base_url:
            from rebar.llm.model_classes import primary_endpoint_for

            if primary_endpoint_for(m, repo_root=cfg.repo_path):
                return m
        if m.startswith(("openai:", "openai-chat:")):
            return f"{_openai_wire_prefix(cfg.base_url)}:{m.split(':', 1)[1]}"
        return m
    from rebar.llm.config import infer_provider

    prov = cfg.model_provider or infer_provider(m, None)
    if prov in {"openai", "openai-chat"}:
        return f"{_openai_wire_prefix(cfg.base_url)}:{m}"
    prefix = _PAI_PROVIDER_PREFIX.get(prov or "", prov)
    return f"{prefix}:{m}" if prefix else m


def _openai_wire_prefix(base_url: str | None) -> str:
    """The hosted-OpenAI wire prefix for a bare/inferred ``openai`` request (ticket 155c):
    ``openai-responses`` by default, ``openai-chat`` when a custom OpenAI-compatible
    ``base_url`` is configured (which rebar can only build as Chat)."""
    return "openai-chat" if base_url else "openai-responses"
