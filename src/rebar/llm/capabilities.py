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
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelCapabilities:
    """The three capability facts rebar's LLM stack branches on — derived from a Pydantic AI
    ``ModelProfile``, never guessed from a provider-name string."""

    native_structured_output: bool
    prompt_cache_style: str  # "none" | "anthropic" | "bedrock"
    supports_thinking: bool


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


# Ordered (predicate, overrides) pairs — NOT a dict keyed by provider name, because a name key
# would reintroduce the string-matching this story removes and could not express a rule spanning
# two hosts of the same model family (direct Anthropic + Bedrock-hosted Anthropic). First
# matching predicate wins; overrides are applied AFTER profile derivation so they take priority.
#
# The (only, so far) entry: Claude's upstream profile claims
# `anthropic_model_profile("claude-opus-4-8").supports_json_schema_output is True` — without this
# override a naive profile read would flip rebar's PRIMARY provider from PromptedOutput to
# NativeOutput, breaking the deliberate choice documented at structured.py and the assertion at
# tests/unit/test_structured.py. A flag-only rule
# (`supports_json_schema_output and not supports_thinking`) was tried and REJECTED: it breaks
# gemini (supports_thinking=True) and groq (supports_json_schema_output=False), both of which get
# NativeOutput today. Do not reintroduce it.
_REBAR_OVERRIDES: tuple[tuple[Any, dict[str, Any]], ...] = (
    (_is_claude, {"native_structured_output": False}),
)


def _capabilities_from_profile(profile: Any) -> ModelCapabilities:
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
    }
    for predicate, overrides in _REBAR_OVERRIDES:
        if predicate(profile):
            caps.update(overrides)
            break
    return ModelCapabilities(
        native_structured_output=bool(caps["native_structured_output"]),
        prompt_cache_style=str(caps["prompt_cache_style"]),
        supports_thinking=bool(caps["supports_thinking"]),
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
    "google-gla": _resolve_google,
    "google-vertex": _resolve_google,
    "vertexai": _resolve_google,
    "google": _resolve_google,
    "google-cloud": _resolve_google,
    "groq": _resolve_groq,
}

_CONSERVATIVE = ModelCapabilities(
    native_structured_output=False, prompt_cache_style="none", supports_thinking=False
)


def capabilities_for(model_or_model_string: Any) -> ModelCapabilities:
    """The :class:`ModelCapabilities` for ``model_or_model_string``.

    Accepts EITHER form (story S1's ``run()`` does not always hold a model object):

    1. an object exposing ``.profile`` -> read that profile;
    2. a provider-qualified model STRING (e.g. ``"openai:gpt-4o"``) -> resolve the vendor
       profile via :data:`_PROFILE_RESOLVERS` WITHOUT constructing a provider;
    3. anything else (unknown provider prefix, malformed string, ...) -> the conservative
       record, logged as exactly ONE warning per call (never raises)."""
    profile = getattr(model_or_model_string, "profile", None)
    if profile is not None:
        return _capabilities_from_profile(profile)

    if isinstance(model_or_model_string, str) and ":" in model_or_model_string:
        provider, _, model_name = model_or_model_string.partition(":")
        resolver = _PROFILE_RESOLVERS.get(provider)
        if resolver is not None:
            try:
                return _capabilities_from_profile(resolver(model_name))
            except Exception:  # noqa: BLE001 — any resolver failure degrades conservatively
                pass

    logger.warning(
        "capabilities_for: could not resolve a ModelProfile for %r — falling back to the "
        "conservative capability record (no native structured output, no prompt caching, "
        "no thinking)",
        model_or_model_string,
    )
    return _CONSERVATIVE


def cache_settings_for(caps: ModelCapabilities) -> Any:
    """The provider-specific prompt-cache ``ModelSettings`` mapping for ``caps``, or ``None``.

    Dispatches SOLELY on ``caps.prompt_cache_style`` — never a provider-name string. Only the
    instructions + tool-definitions cache breakpoints are set (today's behavior); neither
    ``*_cache_messages`` nor ``anthropic_cache`` is touched."""
    if caps.prompt_cache_style == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
    if caps.prompt_cache_style == "bedrock":
        # Imported INSIDE this branch only: `pydantic_ai.models.bedrock` imports `botocore` at
        # module top, so this line executes only when a Bedrock model is actually in use.
        from pydantic_ai.models.bedrock import BedrockModelSettings

        return BedrockModelSettings(
            bedrock_cache_instructions=True,
            bedrock_cache_tool_definitions=True,
        )
    return None
