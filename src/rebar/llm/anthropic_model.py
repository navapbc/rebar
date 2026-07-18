"""Provider/model resolution + Anthropic model construction (leaf).

The provider-agnostic runner (``runner.PydanticAIRunner``) picks a Pydantic AI
model purely from a provider-qualified model string. This module holds that
resolution cluster (``_pai_model`` + the ``_PAI_PROVIDER_PREFIX`` map) together
with the Anthropic-specific construction path the runner funnels through on the
``anthropic:…`` provider: the retrying transport client
(``_build_retrying_anthropic_model``), the loopback-proxy bypass
(``_local_proxy_bypass_base_url``), and the prompt-cache settings
(``_anthropic_cache_settings``).

Heavy libraries (httpx, anthropic, pydantic-ai, tenacity, urllib) are imported
**inside** the functions that need them, never at module top, so this module
keeps the stdlib-only ``import rebar.llm`` contract that ``runner`` relies on.
This is a leaf: it imports nothing back from ``runner``.
"""

from __future__ import annotations

import logging
import os

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)


# Internal provider names -> the Pydantic AI model-string prefix. A small, declarative
# map (NOT per-provider behaviour) so the provider is chosen purely by the model string.
_PAI_PROVIDER_PREFIX = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google_genai": "google-gla",
    "google": "google-gla",
}


def _anthropic_cache_settings(resolved: str):
    """Anthropic-GATED prompt-cache model settings, or ``None`` for any other provider
    (story 0250). ``anthropic_cache_instructions`` puts a ``cache_control`` breakpoint on
    the system-prompt block (the byte-stable parent plan); ``anthropic_cache_tool_definitions``
    caches the tool surface on agentic calls. Both keys live on ``AnthropicModelSettings``
    and error on openai/gemini, so they are emitted ONLY when the resolved model string is
    anthropic-qualified — on every other provider the call is unchanged (no cache_* sent)."""
    if not resolved.startswith("anthropic"):
        return None
    from pydantic_ai.models.anthropic import AnthropicModelSettings

    return AnthropicModelSettings(
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
    )


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


def _build_retrying_anthropic_model(
    name: str, *, base_url: str | None, cfg: LLMConfig, http_timeout=None, _wrapped_transport=None
):
    """Build an ``AnthropicModel`` whose ``AsyncAnthropic`` client carries a retrying
    ``AsyncTenacityTransport`` (story morbid-uncultured-arcticduck). Retry is owned SOLELY by
    the transport (SDK ``max_retries=0``); a construction-time guard fails fast rather than
    silently regress to SDK-managed retries. Returns ``(model, http_client)`` — the caller
    closes ``http_client`` on run teardown via ``asyncio.run(http_client.aclose())``.

    ``base_url=None`` uses the Anthropic SDK default (the normal path); a non-empty value is
    the loopback-proxy-bypass direct URL. ``http_timeout`` is story hoopoe's per-attempt
    ``httpx.Timeout`` when present, else a bounded default from ``cfg.timeout_s`` (never
    unbounded). A transient ``{429,529,5xx}``/``httpx.TimeoutException``/``httpx.NetworkError``
    blip is re-sent BELOW the agent loop, so completed tool calls are never re-executed;
    ``Retry-After`` is honored (capped at ``llm_retry_max_wait_s``), else exponential backoff."""
    import httpx
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
    from tenacity import retry_if_exception_type, stop_after_attempt

    def _validate_response(response: httpx.Response) -> None:
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
    transport = AsyncTenacityTransport(
        config=RetryConfig(
            retry=(
                retry_if_exception_type(httpx.HTTPStatusError)
                | retry_if_exception_type(httpx.TimeoutException)
                | retry_if_exception_type(httpx.NetworkError)
            ),
            wait=wait_retry_after(fallback_strategy=None, max_wait=float(cfg.llm_retry_max_wait_s)),
            stop=stop_after_attempt(attempts),
            reraise=True,
            before_sleep=_before_sleep,
        ),
        # ``_wrapped_transport`` is a test seam (a MockTransport); production uses the real
        # httpx transport.
        wrapped=_wrapped_transport
        if _wrapped_transport is not None
        else httpx.AsyncHTTPTransport(),
        validate_response=_validate_response,
    )
    timeout = http_timeout if http_timeout is not None else httpx.Timeout(float(cfg.timeout_s))
    http_client = httpx.AsyncClient(transport=transport, timeout=timeout)
    anthropic_client = AsyncAnthropic(
        base_url=base_url or None, max_retries=0, http_client=http_client
    )
    # Construction-time guard: never silently regress to SDK-managed retries.
    if anthropic_client.max_retries != 0:
        raise LLMConfigError(
            "transport-retry guard: AsyncAnthropic.max_retries must be 0 "
            "(retry is owned by the httpx transport, not the SDK)"
        )
    model = AnthropicModel(name, provider=AnthropicProvider(anthropic_client=anthropic_client))
    return model, http_client


def _pai_model(cfg: LLMConfig):
    """The Pydantic AI model string for ``cfg`` (provider-qualified). If ``cfg.model``
    already carries a ``provider:`` prefix it is used verbatim; otherwise the provider
    is inferred (or taken from ``cfg.model_provider``) and mapped to Pydantic AI's
    prefix — no per-provider code, the string is the only switch."""
    m = cfg.model
    if ":" in m:
        return m
    from rebar.llm.config import infer_provider

    prov = cfg.model_provider or infer_provider(m, None)
    prefix = _PAI_PROVIDER_PREFIX.get(prov or "", prov)
    return f"{prefix}:{m}" if prefix else m
