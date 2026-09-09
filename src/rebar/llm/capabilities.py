"""Capability decisions read ``ModelProfile``, never a provider-name string (story S2).

Two decisions used to string-match a provider name: ``structured.output_mode()`` consulted a
hardcoded native-output provider frozenset (``structured.py``), and the runner's cache
gate tested a provider-name prefix match on the resolved model string (the old
anthropic-only cache-settings helper).
Both are wrong for Bedrock-hosted Claude, whose model string says ``bedrock`` — so caching
silently switched off and structured output silently took the prompted path. This module is the
single leaf both consumers now read from: a pure mapping of a Pydantic AI ``ModelProfile`` (or a
provider-qualified model string that resolves to one) onto the three capability facts rebar's LLM
stack actually branches on.

Leaf module (the ``anthropic_model.py`` convention): heavy libraries (pydantic_ai, botocore via
the Bedrock settings class) are imported **inside** the functions that need them, never at module
top, so ``import rebar.llm`` stays stdlib-only. This module imports NOTHING from ``runner`` or
``providers``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Conservative fallback for models without a sourced cache minimum. Anthropic's highest
# published minimum avoids warming sub-floor prompts or reporting their zero cache usage as a
# failure. Pass 1 and cache diagnostics share this value through this module.
CACHE_MIN_PREFIX_TOKENS = 4096


# Direct Anthropic minimum cacheable prefix lengths from its prompt-caching documentation,
# read 2026-08-02. Bare model ids cover qualified and unqualified direct requests. Bedrock ids
# use the fallback because Bedrock publishes separate limits and none are recorded here.
_MODEL_CACHE_MIN_PREFIX_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    # claude-opus-4-8 is rebar's DEFAULT_MODEL — the global 4096 was 4x too high here, which
    # is the single most costly instance of the defect e3cd describes.
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-opus-4-1": 1024,
    "claude-mythos-preview": 2048,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
}


def _cache_min_prefix_tokens(prompt_cache_style: str, model_id: str | None) -> int:
    """This model's documented minimum cacheable prefix, else the conservative fallback.

    Gated on ``prompt_cache_style == "anthropic"`` so the DIRECT-Anthropic table is never
    applied to a Bedrock-hosted model of the same family (see the table's own note: Bedrock
    publishes different minimums, and rebar has measured none of them)."""
    if prompt_cache_style == "anthropic" and model_id is not None:
        return _MODEL_CACHE_MIN_PREFIX_TOKENS.get(model_id, CACHE_MIN_PREFIX_TOKENS)
    return CACHE_MIN_PREFIX_TOKENS


@dataclass(frozen=True)
class ModelCapabilities:
    """The capability facts rebar's LLM stack branches on — derived from a Pydantic AI
    ``ModelProfile``, never guessed from a provider-name string."""

    native_structured_output: bool
    prompt_cache_style: str  # "none" | "anthropic" | "bedrock"
    supports_thinking: bool
    # Whether `temperature` may be sent to this model (story S3/2932). Defaults True — the
    # denylist below withdraws it only for the EXACT ids MEASURED to 400 on it, so an unlisted
    # model keeps sending temperature and fails LOUDLY if it turns out to be affected, rather
    # than silently losing Pass-2 greedy determinism for every model as a blanket withdrawal
    # would.
    supports_temperature: bool = True
    # The minimum MARKED-PREFIX size that can cache on this model (bug e3cd). Carried on the
    # capability record alongside `prompt_cache_style` because it is the same kind of fact —
    # per-model, read off the resolved model, never guessed from a provider-name string — and
    # because its two consumers (the cache-effectiveness warning and any prefix-sizing
    # decision) must not each re-derive it. Defaults to the conservative fallback so an
    # unlisted model is never assigned an invented floor.
    cache_min_prefix_tokens: int = CACHE_MIN_PREFIX_TOKENS
    # Whether THIS model can execute web search PROVIDER-SIDE (bug 129e). Read off the
    # profile's `supported_native_tools` registry — the very collection pydantic-ai itself
    # filters native tools against — so it is a MEMBERSHIP test on a capability registry, not a
    # guess from how the provider's name is spelled. rebar attaches web access on every
    # provider regardless (see `web_search_capabilities`); this field only records WHICH ROUTE
    # will serve it, so a signed verdict says whether the reviewer's grounding came from the
    # provider or from the in-process fallback. Defaults False — the conservative record must
    # not claim a provider-side tool it cannot evidence.
    native_web_search: bool = False
    # Whether THIS model accepts a native/json_schema output constraint WHILE extended thinking
    # is on (0fa4). Fail-closed default False: `output_mode` only routes native-under-thinking
    # for a model MEASURED to accept it; every unmeasured model keeps the safe prompted path.
    # The historic blanket "thinking -> prompted" guard rested on a stale Anthropic 400 that was
    # tool_choice x thinking, not outputConfig(json_schema) x thinking (measured E1); this field
    # is the per-model measured replacement, set only by the capability-rows story for cells a
    # live measurement passed.
    native_output_with_thinking: bool = False


def _supports_native_web_search(profile: Any) -> bool:
    """Return whether the profile advertises provider-side web search.

    Membership in ``supported_native_tools`` mirrors pydantic-ai routing and distinguishes
    direct Anthropic from Bedrock without interpreting provider names. Missing dependencies,
    absent profiles, and malformed registries conservatively return ``False``.
    """
    try:
        from pydantic_ai.native_tools import WebSearchTool
    except Exception:  # noqa: BLE001 — no pydantic-ai (lean install) is not evidence of support
        return False
    supported = getattr(profile, "supported_native_tools", None) or ()
    try:
        return isinstance(WebSearchTool(), tuple(supported))
    except Exception:  # noqa: BLE001 — a non-class member degrades to "no provider-side tool"
        return False


# ── Web access for a web-flagged criterion (bug 129e) ──────────────────────────────────────
# Security posture for granting this BLOCKING-gate reviewer web access — the untrusted-content
# exposure accepted, the three bounds (volume/shape/authority), and the deliberately-omitted
# domain controls — is ADR 0063 (docs/adr/0063-web-search-capability-security-posture.md).
_WEB_SEARCH_MAX_USES = 5
"""Provider-side searches allowed per run. Enough for a prior-art sweep (a handful of queries),
far below anything that could flood the reviewer's context or run up a per-search bill."""

_LOCAL_WEB_SEARCH_MAX_RESULTS = 5
"""Result records one local search may return — the local analogue of the bound above, since
`max_uses` rides the native tool and so does not reach the local route."""


def web_search_capabilities(*, web: bool):
    """Return web-search capability for a web-enabled request, otherwise ``None``.

    The decision is independent of provider spelling. Pydantic-ai keeps the native tool when
    the model profile supports it and otherwise uses the bounded DuckDuckGo tool. Returning
    ``None`` leaves unflagged requests unchanged. A missing local-search dependency raises
    instead of removing grounding from a blocking criterion. ADR 0063 defines the content and
    usage bounds.
    """
    if not web:
        return None
    from pydantic_ai.capabilities import WebSearch
    from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
    from pydantic_ai.native_tools import WebSearchTool

    return [
        WebSearch(
            native=WebSearchTool(max_uses=_WEB_SEARCH_MAX_USES),
            local=duckduckgo_search_tool(max_results=_LOCAL_WEB_SEARCH_MAX_RESULTS),
        )
    ]


def _is_claude(profile: Any) -> bool:
    """Direct-Anthropic OR Bedrock-hosted Anthropic.

    Both arms are required: Bedrock-hosted Claude's profile is a ``BedrockModelProfile``, a
    SIBLING class to ``AnthropicModelProfile`` (not a subclass), so the isinstance arm alone
    would miss it. We key on ``bedrock_thinking_variant == "anthropic"`` rather than
    ``isinstance(profile, BedrockModelProfile)`` because that class lives in
    ``pydantic_ai.models.bedrock``, which imports ``botocore`` at module top — an isinstance
    check would drag boto3 onto the always-run capability path (boto3 is a CI-absent
    ``reviewbot``-extra-only dependency)."""
    from pydantic_ai.profiles.anthropic import AnthropicModelProfile

    return isinstance(profile, AnthropicModelProfile) or (
        getattr(profile, "bedrock_thinking_variant", None) == "anthropic"
    )


# Ordered predicates express model-family exceptions without provider-name matching. The first
# match overrides the derived profile. Claude remains on prompted output despite its upstream
# JSON-schema flag. A generic profile-flag rule would change the established Gemini and Groq
# paths.
_REBAR_OVERRIDES: tuple[tuple[Any, dict[str, Any]], ...] = (
    (_is_claude, {"native_structured_output": False}),
)


# Exact model-id overrides record measured exceptions after family defaults. Exact matching
# prevents an unmeasured model from inheriting a capability. Opus 4.7 and 4.8 omit the tunable
# temperature parameter because Bedrock rejects nondefault values while direct Anthropic drops
# them. Regional and global Bedrock ids require separate entries.
_MODEL_ID_CAPABILITY_OVERRIDES: dict[str, Mapping[str, object]] = {
    # Direct Sonnet 4.6 accepted native JSON schema output with and without thinking. Bare ids
    # also cover provider-qualified direct requests. Unmeasured Claude ids retain the family
    # default.
    "claude-sonnet-4-6": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # The direct Anthropic adapter drops Opus 4.8 temperature settings. Recording that fact here
    # avoids both its warning and a false claim that verifier decoding is pinned.
    "claude-opus-4-8": {"supports_temperature": False},
    "claude-opus-4-7": {"supports_temperature": False},
    "us.anthropic.claude-opus-4-8": {"supports_temperature": False},
    "global.anthropic.claude-opus-4-8": {"supports_temperature": False},
    "us.anthropic.claude-opus-4-7": {"supports_temperature": False},
    "global.anthropic.claude-opus-4-7": {"supports_temperature": False},
    # Bedrock measurements showed native JSON schema output works under extended thinking for
    # Sonnet 4.6 and Haiku 4.5. Client-side validation remains required because Bedrock strips
    # numeric bounds from the schema.
    "us.anthropic.claude-sonnet-4-6": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # Bedrock requires the dated Haiku profile id. The bare alias fails request validation.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # Bedrock Opus 4.7 and 4.8 reject structured output under thinking, so their missing rows
    # preserve the conservative family default.
}


def _capabilities_from_profile(profile: Any, model_id: str | None) -> ModelCapabilities:
    native_structured_output = bool(getattr(profile, "supports_json_schema_output", False))
    supports_thinking = bool(getattr(profile, "supports_thinking", False))

    # Capability FIELD presence, not type: a profile exposing `bedrock_supports_prompt_caching`
    # is Bedrock-hosted (regardless of family); else an `AnthropicModelProfile` (SDK-free import)
    # is direct Anthropic; else no prompt caching is wired for this provider.
    if hasattr(profile, "bedrock_supports_prompt_caching"):
        prompt_cache_style = "bedrock"
    else:
        from pydantic_ai.profiles.anthropic import AnthropicModelProfile

        prompt_cache_style = "anthropic" if isinstance(profile, AnthropicModelProfile) else "none"

    caps: dict[str, Any] = {
        "native_structured_output": native_structured_output,
        "prompt_cache_style": prompt_cache_style,
        "supports_thinking": supports_thinking,
        "supports_temperature": True,
        "native_web_search": _supports_native_web_search(profile),
        "native_output_with_thinking": False,
    }
    for predicate, overrides in _REBAR_OVERRIDES:
        if predicate(profile):
            caps.update(overrides)
            break
    # Exact-id override on top (story S3) — a MISSING/None model_id (path 3: no id could be
    # resolved) applies no override, leaving the profile/`_REBAR_OVERRIDES`-derived record as-is.
    if model_id is not None:
        id_overrides = _MODEL_ID_CAPABILITY_OVERRIDES.get(model_id)
        if id_overrides is not None:
            caps.update(id_overrides)
    resolved_cache_style = str(caps["prompt_cache_style"])
    return ModelCapabilities(
        native_structured_output=bool(caps["native_structured_output"]),
        prompt_cache_style=resolved_cache_style,
        supports_thinking=bool(caps["supports_thinking"]),
        supports_temperature=bool(caps["supports_temperature"]),
        native_web_search=bool(caps["native_web_search"]),
        native_output_with_thinking=bool(caps["native_output_with_thinking"]),
        # Derived from the RESOLVED style + id, never from an override table: the floor is a
        # published property of the model, not a rebar policy knob.
        cache_min_prefix_tokens=_cache_min_prefix_tokens(resolved_cache_style, model_id),
    )


# Provider name -> the matching resolver in `pydantic_ai.profiles.*` (pure capability records
# that import WITHOUT the vendor SDK, unlike `pydantic_ai.providers.*` which raise ImportError
# when the vendor package is absent). A MISSING key silently degrades to the conservative
# record — an undisclosed behavior change — so this covers every provider rebar's own
# `anthropic_model._PAI_PROVIDER_PREFIX` can emit. `google-gla`/`google-vertex`/`vertexai` are
# deprecated aliases upstream, but rebar's own prefix map still emits `google-gla`, so omitting
# them would break rebar's own model strings. `bedrock` is deliberately absent: it is a
# rebar-built provider that always arrives as an object (path 1 below), never a bare string, and
# a string resolver for it would need boto3.
def _resolve_anthropic(model_name: str) -> Any:
    from pydantic_ai.profiles.anthropic import anthropic_model_profile

    return anthropic_model_profile(model_name)


def _resolve_openai(model_name: str) -> Any:
    from pydantic_ai.profiles.openai import openai_model_profile

    return openai_model_profile(model_name)


def _resolve_google(model_name: str) -> Any:
    from pydantic_ai.profiles.google import google_model_profile

    return google_model_profile(model_name)


def _resolve_groq(model_name: str) -> Any:
    from pydantic_ai.profiles.groq import groq_model_profile

    return groq_model_profile(model_name)


_PROFILE_RESOLVERS = {
    "anthropic": _resolve_anthropic,
    "openai": _resolve_openai,
    "openai-chat": _resolve_openai,
    "openai-responses": _resolve_openai,
    "google-gla": _resolve_google,
    "google-vertex": _resolve_google,
    "vertexai": _resolve_google,
    "google": _resolve_google,
    "google-cloud": _resolve_google,
    "groq": _resolve_groq,
}

_CONSERVATIVE = ModelCapabilities(
    native_structured_output=False,
    prompt_cache_style="none",
    supports_thinking=False,
    native_output_with_thinking=False,
)


def capabilities_for(model_or_model_string: Any) -> ModelCapabilities:
    """The :class:`ModelCapabilities` for ``model_or_model_string``.

    Accepts EITHER form (story S1's ``run()`` does not always hold a model object):

    1. an object exposing ``.profile`` -> read that profile; its ``.model_name`` (if any) is
       the exact id :data:`_MODEL_ID_CAPABILITY_OVERRIDES` matches against (story S3);
    2. a provider-qualified model STRING (e.g. ``"openai-chat:gpt-4o"``) -> resolve the vendor
       profile via :data:`_PROFILE_RESOLVERS` WITHOUT constructing a provider; the bare model
       name from the SAME ``partition(":")`` already used to pick the resolver is the exact id;
    3. anything else (unknown provider prefix, malformed string, ...) -> the conservative
       record, UNCHANGED (no id is known, so no exact-id override can apply), logged as
       exactly ONE warning per call (never raises)."""
    profile = getattr(model_or_model_string, "profile", None)
    if profile is not None:
        model_id = getattr(model_or_model_string, "model_name", None)
        return _capabilities_from_profile(profile, model_id)

    if isinstance(model_or_model_string, str) and ":" in model_or_model_string:
        provider, _, model_name = model_or_model_string.partition(":")
        resolver = _PROFILE_RESOLVERS.get(provider)
        if resolver is not None:
            try:
                return _capabilities_from_profile(resolver(model_name), model_name)
            except Exception:  # noqa: BLE001 — any resolver failure degrades conservatively
                pass

    logger.warning(
        "capabilities_for: could not resolve a ModelProfile for %r — falling back to the "
        "conservative capability record (no native structured output, no prompt caching, "
        "no thinking)",
        model_or_model_string,
    )
    # An exact-id fact does NOT depend on a profile being resolvable. `bedrock` deliberately
    # has no string resolver (that would need boto3), so a "bedrock:<id>" STRING lands here —
    # and without this, the measured per-model overrides would be silently inert on that path
    # while working on the object path, which is exactly the kind of split-brain that hides a
    # defect until production. Production currently always passes the model OBJECT for a
    # built provider, so this is defence in depth rather than a live bug fix.
    fallback_id = _model_id_of(model_or_model_string)
    id_overrides = _MODEL_ID_CAPABILITY_OVERRIDES.get(fallback_id) if fallback_id else None
    if id_overrides:
        merged: dict[str, Any] = {
            "native_structured_output": _CONSERVATIVE.native_structured_output,
            "prompt_cache_style": _CONSERVATIVE.prompt_cache_style,
            "supports_thinking": _CONSERVATIVE.supports_thinking,
            "supports_temperature": _CONSERVATIVE.supports_temperature,
            "native_web_search": _CONSERVATIVE.native_web_search,
            "native_output_with_thinking": _CONSERVATIVE.native_output_with_thinking,
        }
        merged.update(id_overrides)
        merged_cache_style = str(merged["prompt_cache_style"])
        return ModelCapabilities(
            native_structured_output=bool(merged["native_structured_output"]),
            prompt_cache_style=merged_cache_style,
            supports_thinking=bool(merged["supports_thinking"]),
            supports_temperature=bool(merged["supports_temperature"]),
            native_web_search=bool(merged["native_web_search"]),
            native_output_with_thinking=bool(merged["native_output_with_thinking"]),
            cache_min_prefix_tokens=_cache_min_prefix_tokens(merged_cache_style, fallback_id),
        )
    return _CONSERVATIVE


def _model_id_of(model_or_model_string: Any) -> str | None:
    """The bare model id, from a model object or a ``provider:model`` string; else ``None``."""
    name = getattr(model_or_model_string, "model_name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(model_or_model_string, str) and ":" in model_or_model_string:
        return model_or_model_string.partition(":")[2] or None
    return None


# Enumerated AI Gateway qualifiers use registry membership instead of provider-name parsing.
# A drift test keeps this set aligned with ``config.KNOWN_PROVIDER_NAMES`` and requires each new
# gateway to receive an explicit provenance decision.
_GATEWAY_PROVIDER_NAMES = frozenset(
    {
        "gateway/anthropic",
        "gateway/bedrock",
        "gateway/google-cloud",
        "gateway/groq",
        "gateway/openai",
    }
)


def web_access_provenance(caps: ModelCapabilities, *, web: bool) -> str:
    """How web access was served on this run: ``"native"``, ``"local"``, or ``"off"`` (bug 129e).

    The observability half of the fix. Bug 129e went unnoticed for the whole Bedrock cutover
    because a signed verdict recorded four capability facts and NOTHING about web search, so a
    reader could not tell that a BLOCKING criterion's declared grounding tool had been withdrawn.
    With this on the record, a silent withdrawal is impossible: ``"off"`` on a web-flagged run is
    visible in the verdict itself.

    Three values, not a boolean, because the two ON routes are not equivalent — ``"native"`` means
    the provider searched and no third-party text entered rebar's process, ``"local"`` means it
    did (see the posture comment above ``web_search_capabilities``). The route is read from
    ``caps.native_web_search``, i.e. the same `supported_native_tools` registry pydantic-ai
    filters against, so it reports what pydantic-ai will actually do rather than a second guess."""
    if not web:
        return "off"
    return "native" if caps.native_web_search else "local"


def provenance_for(
    *,
    provider: str,
    model: str,
    base_url: str | None,
    caps: ModelCapabilities,
    web: bool = False,
    bedrock_region_name: str | None = None,
    bedrock_region_source: str | None = None,
    header_names: list[str] | None = None,
) -> dict:
    """Build the provider provenance stored with a signed gate verdict.

    The supplied capabilities are the ones that drove the run and are never recomputed. A
    custom endpoint or enumerated gateway receives ``best_effort`` tier. Unknown gateway hosts
    remain absent because this layer cannot observe them. The effective web flag records the
    native, local, or off route.

    Bedrock region and source use the same resolver as provider construction. Ambient boto3
    resolution remains absent when it cannot be observed here. Header names are sorted and
    values are never persisted. ``urlparse(...).hostname`` excludes embedded credentials from
    the endpoint record.
    """
    from urllib.parse import urlparse

    endpoint_host = urlparse(base_url).hostname if base_url else None
    via_gateway = provider in _GATEWAY_PROVIDER_NAMES
    record: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "endpoint_host": endpoint_host,
        "tier": "best_effort" if (base_url or via_gateway) else "first_class",
        "capabilities": {
            "native_structured_output": caps.native_structured_output,
            "prompt_cache_style": caps.prompt_cache_style,
            "supports_thinking": caps.supports_thinking,
            "supports_temperature": caps.supports_temperature,
            # Bug 129e — see `web_access_provenance`. ``web`` is the request's EFFECTIVE
            # web flag (what the caller actually attached), never `req.web` re-read here, so
            # the record cannot claim access a run did not get.
            "web_access": web_access_provenance(caps, web=web),
        },
    }
    if header_names:
        record["header_names"] = sorted(header_names)
    if provider == "bedrock":
        from rebar.llm.bedrock_model import resolve_bedrock_region

        region, region_source = resolve_bedrock_region(
            bedrock_region_name, configured_source=bedrock_region_source
        )
        if region is not None:
            record["region"] = region
            record["region_source"] = region_source
    return record


# The execution modes that re-send an ACCUMULATED MESSAGE HISTORY and therefore need a
# message-tail cache breakpoint (bug dd27). Membership, not equality, so a future multi-turn
# mode is added here rather than by editing the branch below — and an UNRECOGNISED mode falls
# through to the single-turn arm, which fails safe: a mode whose cost/breakpoint-budget profile
# was never assessed does not silently opt into an extra breakpoint.
_MESSAGE_TAIL_EXECUTION_MODES = frozenset({"agentic"})


def cache_settings_for(caps: ModelCapabilities, *, execution_mode: str) -> Any:
    """Return prompt-cache settings selected only by ``caps.prompt_cache_style``.

    Every caching call marks instructions and tool definitions. Multi-turn modes also mark the
    accumulated message tail to avoid repeatedly sending uncached tool history. The required
    keyword-only ``execution_mode`` prevents a new caller from selecting a policy implicitly.

    Anthropic uses mutually exclusive automatic ``anthropic_cache`` settings. Bedrock uses
    ``bedrock_cache_messages`` and must not receive top-level ``cache_control``. Both paths stay
    within their breakpoint limits. Single-turn mappings remain unchanged.
    """
    cache_message_tail = execution_mode in _MESSAGE_TAIL_EXECUTION_MODES
    if caps.prompt_cache_style == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        anthropic_settings = AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
        if cache_message_tail:
            anthropic_settings["anthropic_cache"] = True
        return anthropic_settings
    if caps.prompt_cache_style == "bedrock":
        # Imported INSIDE this branch only: `pydantic_ai.models.bedrock` imports `botocore` at
        # module top, so this line executes only when a Bedrock model is actually in use.
        from pydantic_ai.models.bedrock import BedrockModelSettings

        bedrock_settings = BedrockModelSettings(
            bedrock_cache_instructions=True,
            bedrock_cache_tool_definitions=True,
        )
        if cache_message_tail:
            bedrock_settings["bedrock_cache_messages"] = True
        return bedrock_settings
    return None
