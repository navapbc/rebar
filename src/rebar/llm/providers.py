"""Per-run provider construction seam (story S1 / one-provider-factory).

Provider construction used to be an inline special case in ``runner.run()``: an
``if resolved.startswith("anthropic")`` branch that built the retrying Anthropic
model directly, with a matching ad-hoc ``_http_client`` teardown in the run's
``finally``. Any second provider needing custom construction (its own retrying
transport, a bespoke client, etc.) would have had to add another branch in that
same spot — there was no single place that answered "how is a Provider built for
provider X". ``ProviderSession`` is that place: one per-run object holding a
registry of ``provider_name -> builder`` and exposing ``provider_factory``, the
exact ``(str) -> Provider`` hook pydantic-ai's ``infer_model(provider_factory=...)``
calls (verified against pydantic-ai 1.107.1: ``infer_model`` parses the model
string into ``(provider_name, model_name)`` and calls
``provider = provider_factory(provider_name)`` with the BARE provider name, e.g.
``"anthropic"`` — never the full ``"anthropic:claude-..."`` model string).

It is a SESSION, not a bare function, because the hook's return type carries only
a ``Provider`` — there is no channel back to the caller for the retrying
``httpx.AsyncClient`` a builder opens alongside it. Each builder that opens one
appends it to ``self._closeables``; the session is a context manager, so
``runner.run()`` wraps its body in ``with ProviderSession(cfg) as session:`` and
``__exit__`` closes everything the builders opened — the old ad-hoc
``_http_client`` local and its bespoke ``finally`` teardown disappear. One session
per ``run()`` call, never shared across runs or threads, so ``_closeables`` needs
no locking.

Leaf module (matches the ``anthropic_model.py`` convention): imports nothing from
``runner``; heavy libraries (httpx, pydantic-ai) are imported **inside** the
functions that need them, never at module top, so ``import rebar.llm`` stays
stdlib-only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

# Deprecated provider aliases pydantic-ai's OWN `infer_provider` (1.107.1,
# providers/__init__.py) special-cases BEFORE the name->class lookup that backs
# `known_model_names()` below — so they resolve fine at runtime but are absent
# from the `KnownModelName` literal `known_model_names()` enumerates. Listed
# explicitly so they are not misreported as unknown by `_pydantic_ai_recognizes`.
_DEPRECATED_PROVIDER_ALIASES = frozenset({"google-gla", "google-vertex", "vertexai"})


@lru_cache(maxsize=1)
def _pydantic_ai_known_providers() -> frozenset[str]:
    """The provider names pydantic-ai itself recognizes, derived CHEAPLY.

    ``pydantic_ai.models.known_model_names()`` introspects the ``KnownModelName``
    ``Literal`` alias — pure type introspection, no provider-specific import — so
    calling it never requires an optional provider package (openai/google/...) to
    be installed, unlike actually asking pydantic-ai to classify/construct a
    provider (``infer_provider_class``/``infer_provider`` import the matching
    ``pydantic_ai.providers.<name>`` submodule as soon as the name is recognized,
    which is exactly the cost this function lets callers avoid paying for a name
    they are not about to build).
    """
    import pydantic_ai.models as pai_models

    provider_names = {
        name.split(":", 1)[0] for name in pai_models.known_model_names() if ":" in name
    }
    return frozenset(provider_names) | _DEPRECATED_PROVIDER_ALIASES


class ProviderSession:
    """One provider-construction + teardown scope for a single ``run()`` call.

    ``provider_factory`` is the callable to hand ``pydantic_ai.models.infer_model``
    (or ``Agent``, which forwards to it): the ONE place that answers "how is a
    Provider built for provider X", including when the answer is "it isn't". A
    rebar-registered provider (``"anthropic"``/``"bedrock"``) is built by rebar's own
    builder; any other name pydantic-ai itself recognizes is delegated to its
    default resolution (``pydantic_ai.providers.infer_provider``) unchanged from
    before this seam existed; a name neither side recognizes raises the typed
    :class:`LLMConfigError` rather than a bare ``KeyError``/``ValueError``.

    ``is_resolvable`` is the cheap, side-effect-free companion query: whether THIS
    call would succeed, checked WITHOUT constructing (or importing the optional
    package behind) anything. ``runner.run()`` uses it to decide whether to eagerly
    resolve a model through this factory at all — a delegate provider it does not
    own (openai/google/...) is left as a lazy model STRING for pydantic-ai's own
    ``Agent`` construction to resolve later, exactly as it always was, rather than
    forcing that provider's optional package to be importable just to wire up
    ``model_settings`` before any real call is made. This session adds no coverage
    beyond the anthropic path it relocates — it only makes "not ours to build" a
    checkable fact instead of an assumption.
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        # Clients opened by a builder this run, closed in `close()` / `__exit__`.
        # Never shared/mutated across runs or threads, so no lock is needed.
        self._closeables: list[Any] = []
        # The endpoint of the candidate currently being built (task cc33). Only `model_for`
        # sets it; it is None for every other path, so the builders behave exactly as before.
        self._endpoint_override: str | None = None
        self._builders: dict[str, Callable[[str], Any]] = {
            "anthropic": self._build_anthropic,
            "bedrock": self._build_bedrock,
        }
        # The `openai` builder is registered ONLY when `cfg.base_url` is set (story S4):
        # with no base_url, rebar has nothing to contribute (no endpoint to inject, no key
        # to place, no profile to override), so it must NOT interpose — the model stays a
        # lazy STRING on the deferred path `is_resolvable()` already provides, exactly as
        # before this story. Registering it unconditionally would also break the existing
        # parameterized `openai:gpt-4o` case in test_pydantic_ai_runner.py, which stubs
        # `_pai_structured` so no provider is ever constructed there.
        if cfg.base_url:
            self._builders["openai"] = self._build_openai

    def supports(self, provider_name: str) -> bool:
        """True if ``provider_name`` has a rebar builder registered on this session."""
        return provider_name in self._builders

    def is_resolvable(self, provider_name: str) -> bool:
        """True if `provider_factory(provider_name)` would succeed — checked WITHOUT
        constructing (or importing the optional package behind) anything: a rebar
        builder is registered, OR pydantic-ai's own ``known_model_names()`` (cheap
        type introspection, see ``_pydantic_ai_known_providers``) recognizes the
        name."""
        return provider_name in self._builders or provider_name in _pydantic_ai_known_providers()

    def provider_factory(self, provider_name: str) -> Any:
        """The ``(str) -> Provider`` hook passed as ``infer_model(provider_factory=...)``.

        The one place that answers "how is a Provider built for provider X",
        including when the answer is "it isn't". Three steps:

        1. ``provider_name`` has a rebar builder registered (``"anthropic"``/
           ``"bedrock"``) -> run it.
        2. Otherwise, if pydantic-ai itself recognizes the name (``is_resolvable``),
           delegate to its OWN default resolution (``pydantic_ai.providers.infer_provider``
           — the same function ``infer_model``'s ``provider_factory`` parameter
           defaults to). This is what keeps ``openai:``/``google-gla:``/... working:
           rebar registers no builder for them, so they get exactly what pydantic-ai
           would have built anyway — no new capability, no regression.
        3. Neither resolves it -> a genuine unknown/misspelled provider name, which
           is an operator CONFIGURATION error, not a provider outage. (Belt-and-braces:
           pydantic-ai's ``infer_provider`` itself raises a bare ``ValueError`` for an
           unrecognized name — verified against 1.107.1's ``infer_provider_class``,
           its ``else: raise ValueError(...)`` branch — so step 2's call is ALSO
           wrapped, in case ``is_resolvable``'s cheap check and the real resolver ever
           disagree.) Translated into :class:`LLMConfigError`, chaining the upstream
           error as the cause and naming both the offending name and rebar's
           registered providers. This must never surface as ``LLMUnavailableError``
           (run()'s broad-except path for a reached-but-failed provider) — that would
           tell the operator the provider was reached and failed, the opposite of
           what happened, and would be indistinguishable from a real outage/auth
           failure.
        """
        builder = self._builders.get(provider_name)
        # A per-candidate endpoint (task cc33) is rebar's to inject, so it registers the
        # OpenAI-compatible builder for this build even when `cfg.base_url` is unset.
        if builder is None and provider_name == "openai" and self._endpoint_override:
            builder = self._build_openai
        if builder is not None:
            return builder(provider_name)

        if provider_name in _pydantic_ai_known_providers():
            from pydantic_ai.providers import infer_provider

            try:
                return infer_provider(provider_name)
            except ValueError as exc:
                raise self._unknown_provider_error(provider_name) from exc

        raise self._unknown_provider_error(provider_name)

    def _unknown_provider_error(self, provider_name: str) -> LLMConfigError:
        return LLMConfigError(
            f"unknown provider {provider_name!r}; registered providers: {sorted(self._builders)}"
        )

    def _build_anthropic(self, provider_name: str) -> Any:  # noqa: ARG002 — hook signature
        """Verbatim relocation of the inline ``resolved.startswith("anthropic")``
        branch that used to live in ``runner.run()`` (story arcticduck / hoopoe):
        the retrying transport and the SDK ``max_retries=0`` guard are still owned by
        ``_build_retrying_anthropic_model`` — this only relocates the call site and
        hands the opened client to the session instead of a local ``_http_client``.

        ``base_url`` runs every anthropic model through
        ``_local_proxy_bypass_base_url`` (bug sue-skimp-tear): a LOCAL Claude-Code
        payload optimizer (e.g. headroom on ``127.0.0.1``), inherited via a loopback
        ``ANTHROPIC_BASE_URL``, corrupts rebar's own multi-turn agentic tool-loop
        requests into an empty provider stream, collapsing the plan-review/completion
        verifiers to INDETERMINATE — so rebar's internal agent bypasses it and talks
        to the direct public API instead; a real (non-loopback) gateway is respected,
        and ``REBAR_LLM_ALLOW_LOCAL_PROXY=1`` opts back in. ``http_timeout`` is the
        per-request READ timeout (story hoopoe), reusing ``cfg.timeout_s``.

        ``_build_retrying_anthropic_model`` returns ``(model, client)``; the
        hook's contract is a ``Provider``, so the client is registered here for
        teardown and only ``model.provider`` (the ``AnthropicProvider`` wrapping
        the retrying ``AsyncAnthropic``) is returned.
        """
        import httpx

        from rebar.llm.anthropic_model import (
            _build_retrying_anthropic_model,
            _local_proxy_bypass_base_url,
            _pai_model,
        )

        cfg = self._cfg
        resolved = _pai_model(cfg)
        name = resolved.split(":", 1)[1] if ":" in resolved else resolved
        # A candidate's own `endpoint` wins over the loopback-bypass default: it was chosen
        # explicitly (a local proxy backing a hosted primary is the point of a chain).
        direct = self._endpoint_override or _local_proxy_bypass_base_url()
        http_timeout = httpx.Timeout(read=float(cfg.timeout_s), connect=10.0, write=30.0, pool=10.0)
        model, client = _build_retrying_anthropic_model(
            name, base_url=direct, cfg=cfg, http_timeout=http_timeout
        )
        self._closeables.append(client)
        return model.provider

    def _build_bedrock(self, provider_name: str) -> Any:  # noqa: ARG002 — hook signature
        """One-line delegation to the ``bedrock_model`` leaf builder (story S3/2932) — the
        construction logic lives THERE, not here: this file budgets itself well under the
        module-size cap (story S3/2932 held it at ~289 lines), so only the registry entry —
        never a provider's construction body — belongs in ``ProviderSession``."""
        from rebar.llm.bedrock_model import build_bedrock_provider

        return build_bedrock_provider(self._cfg)

    def _build_openai(self, provider_name: str) -> Any:  # noqa: ARG002 — hook signature
        """Build an OpenAI-COMPATIBLE provider for ``cfg.base_url`` (story S4) — the seam
        that makes the ``docs/llm-framework.md`` local-server recipe (LMStudio/Ollama/vLLM/a
        LiteLLM proxy) real instead of a documented-but-refused promise. Sends a placeholder
        key (``"not-needed"``) when ``cfg.api_key`` is unset so construction succeeds against
        a no-auth local server — a real hosted endpoint still answers with its own 401.
        ``pydantic-ai-slim[openai]`` is optional (kept out of the lean default install; see
        pyproject.toml); imported HERE so an absent extra surfaces as a named
        :class:`LLMConfigError`, not a bare ``ModuleNotFoundError`` deep in pydantic-ai."""
        try:
            import dataclasses

            import httpx
            from pydantic_ai.profiles.openai import openai_model_profile
            from pydantic_ai.providers.openai import OpenAIProvider
        except ImportError as exc:
            raise LLMConfigError(
                "an OpenAI-compatible base_url is configured but the optional openai "
                "provider package is not installed: pip install 'pydantic-ai-slim[openai]'"
            ) from exc

        class _RebarOpenAICompatibleProvider(OpenAIProvider):  # type: ignore[misc]
            """Withdraws ONLY ``supports_json_schema_output`` from upstream's OpenAI profile
            (story S4 AC5) — nothing else about the profile is touched. An opaque
            OpenAI-*compatible* endpoint has no obligation to implement strict ``json_schema``
            decoding, but ``openai_model_profile(...)`` describes OpenAI's HOSTED API
            (``True``), which would otherwise steer ``capabilities_for()`` (story S2) onto
            ``NativeOutput`` and fail opaquely. Rides the Provider, not a model argument:
            ``OpenAIChatModel.__init__`` resolves ``profile=profile or provider.model_profile``
            — this factory hands ``infer_model`` a Provider, never a Model, so overriding this
            staticmethod is the only reachable seam. Verified against the pinned pydantic-ai:
            survives the ``OpenAIModelProfile(...).update()`` applied afterwards, leaving
            ``supports_tools`` untouched."""

            @staticmethod
            def model_profile(model_name: str):
                base = openai_model_profile(model_name)
                return dataclasses.replace(base, supports_json_schema_output=False)

        cfg = self._cfg
        # Build our OWN client wrapping `httpx.AsyncHTTPTransport()` explicitly: pydantic-ai's
        # `create_async_http_client()` (used when no `http_client=` is passed) builds a bare
        # `httpx.AsyncClient(...)`, so httpx picks its default transport WITHOUT going through
        # the `httpx.AsyncHTTPTransport` module attribute — unreachable by the same
        # `monkeypatch.setattr(httpx, "AsyncHTTPTransport", ...)` seam
        # `_build_retrying_anthropic_model` relies on for its own tests. Also gives the same
        # bounded per-request timeout (`cfg.timeout_s`) local-server config already promises.
        http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(), timeout=httpx.Timeout(float(cfg.timeout_s))
        )
        self._closeables.append(http_client)
        return _RebarOpenAICompatibleProvider(
            base_url=self._endpoint_override or cfg.base_url,
            api_key=cfg.api_key or "not-needed",
            http_client=http_client,
        )

    def model_for(self, resolved: str, *, endpoint: str | None = None) -> Any:
        """Build the pydantic-ai Model for ONE ``provider:model`` candidate, optionally against
        that candidate's own ``endpoint``, registering anything it opens on THIS session.

        The entry point a fallback chain (task cc33) builds each of its N+1 candidates through,
        so one session owns every rebar-created transport for the run — a session per candidate
        would close only its own client and leak the rest. The override is scoped to the single
        build and restored afterwards, so the session stays reusable for the un-overridden path."""
        from pydantic_ai.models import infer_model

        previous = self._endpoint_override
        self._endpoint_override = endpoint
        try:
            return infer_model(resolved, provider_factory=self.provider_factory)
        finally:
            self._endpoint_override = previous

    def close(self) -> None:
        """Close every client a builder opened this run (story arcticduck).

        ``aclose()`` is async; after the run's own event loop is gone,
        ``asyncio.run`` closes the connection pool from this synchronous caller.
        Best-effort — cleanup never raises out of teardown."""
        import asyncio

        for client in self._closeables:
            try:
                asyncio.run(client.aclose())
            except Exception:  # noqa: BLE001 — teardown is best-effort; log, never raise
                logger.warning("llm transport client aclose failed on teardown", exc_info=True)

    def __enter__(self) -> ProviderSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
