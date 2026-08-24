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


# The CONSERVATIVE fallback minimum prompt-prefix the anthropic cache will write/read, used
# for any model with no documented per-model figure (see `_MODEL_CACHE_MIN_PREFIX_TOKENS`
# below for the documented ones). Below the applicable floor a prefix never caches, so a
# zero/zero cache reading is the EXPECTED result rather than a symptom of anything.
#
# Lives HERE, alongside ``cache_settings_for``, because it is a fact about the prompt cache
# that BOTH sides need and neither owns (bug 7a79): the Pass-1 warm-up decision
# (``llm/plan_review/pass1.py`` — warming a sub-floor prefix would add a serialized call for
# no read benefit, story ba7e) and the cache-effectiveness warning
# (``llm/structured_run.py:warn_if_cache_ineffective`` — it can only claim caching FAILED for
# a prompt that was cacheable to begin with). ONE definition; do not restate the literal.
#
# 4096 is the HIGHEST value in Anthropic's published table, which makes it the conservative
# choice for an unlisted model: too high merely under-warns (a missed signal), whereas too
# low re-creates the unactionable warning spam bug 7a79 removed. Bug e3cd renamed what this
# constant means — it is the FALLBACK, no longer "the floor" — because applying it to every
# model made it 4x too high on rebar's own DEFAULT_MODEL.
CACHE_MIN_PREFIX_TOKENS = 4096


# Anthropic's PUBLISHED per-model minimum cacheable prompt length. Transcribed from
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching
# (§ "Minimum cacheable prompt length", read 2026-08-02) — the documentation is the source of
# truth here. Independent empirical brackets recorded on bug e3cd VERIFY these values rather
# than defining them:
#
#   sonnet-4-6    922 -> no cache, 1042 -> write 1035 / read 1035     => ~1024   ✓ doc 1024
#   sonnet-4-5    253 -> no cache, 7930 -> cached                     => <=1024  ✓ doc 1024
#   haiku-4-5    2748 -> no cache, 4749 -> write 4742 / read 4742     => (2748,4749] ✓ doc 4096
#
# NOT monotonic across generations — 512 on the newest models but 4096 on opus-4-6/4-5 and
# haiku-4-5 — which is precisely why a single global constant could not express it.
#
# Keyed on the BARE model id (`_model_id_of` strips the provider prefix), so these cover both
# `anthropic:claude-opus-4-8` and the bare string. Bedrock ids (`us.anthropic.…`,
# `global.anthropic.…`) are DELIBERATELY absent: Anthropic's table states these minimums apply
# on every platform EXCEPT Amazon Bedrock, which documents its own minimums separately. Rather
# than guess a Bedrock number, a Bedrock model falls through to the conservative fallback —
# the "if you cannot source it confidently, do not invent it" rule this bug was filed under.
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
    """Does this model's profile list a provider-side web-search tool? (bug 129e)

    MEMBERSHIP in ``profile.supported_native_tools``, tested exactly the way pydantic-ai's own
    request builder tests it (``models/__init__.py``: an unsupported native tool is dropped when a
    local fallback exists, kept otherwise) — so this predicts pydantic-ai's routing from the same
    registry pydantic-ai reads, rather than re-deriving it from the provider's NAME. That is the
    whole point: `bedrock:us.anthropic.claude-opus-4-8` and `anthropic:claude-opus-4-8` are the
    same MODEL reached through different providers, and only the registry knows they differ here.

    MEASURED (ticket 129e, real Bedrock haiku call + a no-capability control on the same model):
    Bedrock's Converse API cannot carry Anthropic's server-side web_search — the live call raised
    ``UserError: Native tool(s) ['WebSearchTool'] not supported by this model. Supported: []``.
    The static read agrees: ``BedrockProvider.model_profile('us.anthropic.claude-opus-4-8')
    .supported_native_tools`` is empty, while the direct-Anthropic profile lists ``WebSearchTool``.

    Never raises: an unresolvable/absent profile is simply not evidence of a provider-side tool."""
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
    """The pydantic-ai capability list granting web access to THIS request, or ``None``.

    Takes NO model/provider argument, by design. Web access is required for a web-flagged
    criterion on EVERY provider and model (operator decision, bug 129e), so nothing about the
    provider is an input to the decision — and a function handed no model string demonstrably
    cannot gate on the shape of one. This replaces an ``anthropic``-prefix gate that withdrew
    T1's grounding tool the moment the production bot moved to Bedrock: same model, different
    provider name, capability silently gone.

    ``web=False`` (every criterion whose routing entry does not declare ``"web": true``, and the
    injected-test-model path) returns ``None`` and the caller omits the ``capabilities`` kwarg
    entirely, so an unflagged request stays byte-identical to the pre-129e wire format.

    ONE ``WebSearch`` covers both routes: pydantic-ai keeps the provider's native tool where the
    model's profile supports it (cheaper, provider-grounded, no third-party content entering
    rebar's process) and drops it in favour of the local tool where it does not. That is why this
    lives HERE rather than in ``anthropic_model.py``: it is no longer an Anthropic construction
    detail but a capability decision, the same kind ``cache_settings_for`` makes, and this module
    is the one that derives those from capability facts instead of provider names.

    Bounds and the untrusted-content tradeoff are argued in ADR 0063 (web-search capability
    security posture) — read it before widening anything here.

    Raises ``pydantic_ai.exceptions.UserError`` if the ``duckduckgo`` optional group is missing
    (the local tool resolves EAGERLY at construction). That is deliberate: a web-flagged blocking
    criterion silently losing its grounding is the bug being fixed, so an unprovisioned
    environment must fail loudly rather than degrade. The group ships in the ``agents`` extra and
    in the review-bot image."""
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


# Exact-model-id capability overrides (story S3/2932) — a SEPARATE table from
# `_REBAR_OVERRIDES` above, not a widening of it: that table's predicate is
# `Callable[[ModelProfile], bool]`, a SIGNED contract of the closed S2 story, and a leaf must
# not redefine a signed upstream contract. Keyed on the EXACT model id — never prefix
# matching (this module must contain no prefix-match call, an attested S2 criterion), so
# this table also cannot speculate about an unmeasured model's behavior. Applied AFTER
# `_REBAR_OVERRIDES` so an id-specific MEASURED fact always wins over the family-level default.
#
# MEASURED (ticket 2932, real AWS us-east-2): `us.anthropic.claude-opus-4-7` + `temperature=0`
# returns HTTP 400 "temperature is deprecated for this model"; the IDENTICAL call without
# temperature succeeds. pydantic-ai's `_drop_unsupported_sampling_settings` exists only in its
# Anthropic adapter, not its Bedrock one, so the direct-Anthropic path degrades with a warning
# but Bedrock hard-fails — and rebar's Pass-2 verifiers deliberately send `temperature=0`, so
# leaving this model at the default would break Bedrock gate runs on it.
# MEASURED matrix (ticket 1903, account 896586841071 / us-east-1, boto3 converse, maxTokens 8),
# recorded so the next reader does not re-derive it:
#
#   model id                          temp unset | temp=0.0 | temp=0.5 | temp=1.0 | topP=0.9
#   us./global. claude-sonnet-4-6     OK         | OK       | OK       | -        | -
#   us./global. claude-opus-4-8       OK         | 400      | 400      | OK       | 400
#   us./global. claude-opus-4-7       OK         | 400      | -        | -        | -
#
# NOTE the shape: this is NOT "temperature unsupported" — temp=1.0 (the API default) SUCCEEDS on
# opus-4-8 while 0.0 and 0.5 fail, so what is deprecated is the parameter's TUNABILITY. Withdrawing
# the parameter is still correct (the model then uses its own default), but the field name
# understates the mechanism. `top_p` is deprecated on opus-4-8 too; that stays LATENT because rebar
# never sets it (failure.py's _SAMPLING_PARAMS anticipates it).
#
# Keyed on the FULL model id, so each profile prefix needs its OWN entry — a `us.` entry does not
# cover its `global.` twin. claude-opus-4-8 matters most: it is rebar's DEFAULT_MODEL.
_MODEL_ID_CAPABILITY_OVERRIDES: dict[str, Mapping[str, object]] = {
    # The DIRECT-ANTHROPIC forms. The table keys on the BARE id (`_model_id_of` strips the
    # provider prefix), so these cover both `anthropic:claude-opus-4-8` and the bare string.
    # MEASURED on ticket 8fbd-e9d5-326e-40d8: direct `claude-sonnet-4-6` accepted native JSON
    # schema output both without thinking and with a 1024-token thinking budget, returning a
    # valid structured result and a thinking part. This exact-id row makes that measured arm
    # symmetric with the separately measured Bedrock inference-profile row below; unmeasured
    # direct Claude ids still inherit the conservative family default.
    "claude-sonnet-4-6": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # OBSERVED IN PRODUCTION on the code-review bot: pydantic-ai's Anthropic adapter emits
    # "Sampling parameters ['temperature'] are not supported by 'claude-opus-4-8'. These settings
    # will be ignored." on essentially EVERY call (models/anthropic.py:641, via
    # `_drop_unsupported_sampling_settings`). Its Bedrock adapter has NO such drop, which is why
    # the same model 400s there and merely warns here — same defect, two symptoms.
    # The warning is the visible half; the REAL cost is that a pass pinning temperature=0 for
    # determinism silently does not get it (code-review.yaml pins it on Pass-2 verify so that
    # re-running a finding cannot resample its verdict, and code review has no verifier downgrade,
    # so ALL its passes run on this model). Withdrawing the parameter here is wire-identical to
    # having it dropped downstream, minus the per-call warning and minus the false belief that
    # greedy decoding is in effect.
    "claude-opus-4-8": {"supports_temperature": False},
    "claude-opus-4-7": {"supports_temperature": False},
    "us.anthropic.claude-opus-4-8": {"supports_temperature": False},
    "global.anthropic.claude-opus-4-8": {"supports_temperature": False},
    "us.anthropic.claude-opus-4-7": {"supports_temperature": False},
    "global.anthropic.claude-opus-4-7": {"supports_temperature": False},
    # MEASURED native structured output UNDER extended thinking (run E1, recorded on ticket
    # df3a "EXPERIMENT RESULTS" comment: 39 live Bedrock calls, AWS account 896586841071 /
    # us-east-1; outputConfig json_schema + extended thinking proven wire-legal — sonnet-4-6
    # adaptive, haiku-4-5 budget 2048; no 400). Client-side Pydantic validators remain MANDATORY:
    # Bedrock's schema transform strips ge/le bounds (E1 emitted a 1.0 out-of-bounds value).
    "us.anthropic.claude-sonnet-4-6": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # The DATED haiku profile id — the bare alias 400s at request validation.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "native_structured_output": True,
        "native_output_with_thinking": True,
    },
    # NON-ROW: Bedrock opus-4-7 / opus-4-8 are rejected server-side by Converse structured
    # output under thinking (provider rollout gap; re-measure when AWS announces support), so
    # they get NO native-output row and stay fail-closed (their supports_temperature rows above
    # are a separate ticket's fact and untouched here).
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


# The provider qualifiers that route a run through Pydantic's AI Gateway, ENUMERATED (bug 7fe2).
#
# Membership, never prefix matching. This module exists precisely because provider-name string
# matching is wrong (see the module docstring), f184's attested criterion forbids prefix matching
# here, and epic 061c's standing decision is provider qualification by REGISTRY MEMBERSHIP with
# no exceptions — LiteLLM's Bedrock id-sniffing is the recurring bug class that rule exists to
# avoid. Membership is also strictly better on the merits: the set is auditable at a glance, and
# an unrecognized `gateway/...` string is not silently granted gateway semantics.
#
# These are exactly the ``gateway/*`` members of ``config.KNOWN_PROVIDER_NAMES`` — the registry
# that decides whether a qualifier is admissible at all. They are restated rather than filtered
# out of it because filtering would itself require the banned prefix test; a drift test pins the
# two in lockstep, so a sixth gateway added to the registry fails the build here until listed.
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
) -> dict:
    """The ``provider_provenance`` record for a signed gate verdict (story S5/343b).

    A verdict used to record only a model STRING, so a run behind an opaque gateway still
    claimed it came from ``anthropic:claude-opus-4-8``. This assembles the additive record
    that names the resolved provider/model, the endpoint actually called (``tier`` flips to
    ``"best_effort"`` once a custom ``base_url`` is set OR the provider is a known gateway
    qualifier — see below), and the EFFECTIVE capability record
    that drove the run — carried through from the ``caps`` argument, never recomputed (a
    second `capabilities_for` resolution here could diverge from the record that actually
    drove the run, which is exactly the prior regression this must not repeat).

    GATEWAY QUALIFIERS (bug 7fe2). Deriving the tier from ``base_url`` ALONE was wrong: the
    five ``gateway/*`` names in ``config.KNOWN_PROVIDER_NAMES`` are live (pydantic-ai's
    ``infer_provider`` resolves them) and carry NO ``base_url``, because the gateway URL is
    resolved inside pydantic-ai from its own env/API key. So a ``gateway/anthropic`` run — every
    byte of which traverses an intermediary that can rewrite the request; the Vercel AI Gateway
    has been documented silently downgrading Anthropic's 1-hour prompt cache — signed as
    ``first_class``. Gateway membership is therefore a second, independent trigger for
    ``best_effort``, decided by MEMBERSHIP in :data:`_GATEWAY_PROVIDER_NAMES` (never by prefix or
    substring shape — see that constant), so a direct provider whose name merely contains the
    token is not collaterally downgraded.

    ``endpoint_host`` is deliberately NOT back-filled with a guessed gateway hostname. This seam
    never observes the resolved gateway URL (pydantic-ai reads ``PYDANTIC_AI_GATEWAY_BASE_URL`` /
    ``PAIG_BASE_URL``, or infers it from the API key), and synthesising ``gateway.pydantic.dev``
    would place an UNVERIFIED fact into a SIGNED record — exactly what the tier field exists to
    prevent. The intermediary is instead named by the OBSERVED ``provider`` field
    (``gateway/anthropic``) and flagged by ``tier``. When a ``base_url`` IS configured its real
    host is recorded, gateway or not.

    ``web`` is the request's EFFECTIVE web flag (bug 129e) — whether the caller actually attached
    ``web_search_capabilities`` to this run, not the criterion's declared intent. It is recorded
    (with its route) under ``capabilities.web_access``; see ``web_access_provenance``.

    BEDROCK REGION (bug 8274). On a ``"bedrock"`` run the record additionally carries ``region``
    + ``region_source`` when rebar's OWN chain resolved the region —
    ``bedrock_model.resolve_bedrock_region``, the same pure stdlib-only resolver the provider
    builder feeds to the boto3 session, so record and runtime cannot disagree. Both keys are
    ABSENT (never guessed) when only boto3's ambient profile resolution could supply one: this
    seam never imports boto3, so that value is unobservable here, and synthesising it would put
    an unverified fact into a signed record — the exact rule ``endpoint_host`` follows above.
    ``bedrock_region_name`` is threaded unconditionally by the runner; the provider check gates
    the record, so a non-bedrock verdict never carries region keys. ``bedrock_region_source``
    (cda8) is the configured knob's TRUE origin — ``LLMConfig.bedrock_region_source``, resolved
    by the SAME ``from_env`` pass that produced the value — so a ``[tool.rebar.llm]`` pin is
    labeled ``repo-config`` (and a CLI override ``cli``), never blanket-labeled as the env var.

    Security: the host is read via ``urlparse(base_url).hostname``, never ``.netloc`` — the
    latter retains embedded credentials (``user:secret@host``), and no credential material may
    appear in a signed record."""
    from urllib.parse import urlparse

    endpoint_host = urlparse(base_url).hostname if base_url else None
    via_gateway = provider in _GATEWAY_PROVIDER_NAMES
    record = {
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
    """The provider-specific prompt-cache ``ModelSettings`` mapping for ``caps``, or ``None``.

    Dispatches SOLELY on ``caps.prompt_cache_style`` — never a provider-name string.

    The instructions + tool-definitions breakpoints are set on every caching call. On a
    MULTI-TURN mode (:data:`_MESSAGE_TAIL_EXECUTION_MODES`) a THIRD breakpoint covers the
    accumulated message tail. Bug dd27: without it the growing tool-result history rode outside
    every breakpoint and was re-sent uncached on each turn, making input cost O(N^2) in turns —
    and invisible to ``structured_run.warn_if_cache_ineffective``, whose predicate requires
    ``cache_read_tokens == 0`` while the SYSTEM block was hitting cache all along. Anthropic's
    own prompt-caching guidance names this the multi-turn shape (system + a point the
    conversation can be incrementally cached against), not an exotic optimisation.

    ``execution_mode`` is keyword-only and REQUIRED on purpose. A default would let a new call
    site silently pick an arm, which is the exact failure being fixed; the caller always knows
    which mode it is on (``RunRequest.execution_mode``).

    PROVIDER ASYMMETRY — the trap. The two arms must use their OWN keys:

    * Anthropic — ``anthropic_cache=True``, the automatic-caching key, which sends a TOP-LEVEL
      ``cache_control`` so the server places the breakpoint at the end of the prompt. It is
      mutually exclusive with ``anthropic_cache_messages`` (pydantic-ai raises ``UserError`` if
      both are set), so only one of the pair is ever emitted.
    * Bedrock — ``bedrock_cache_messages=True``. There is NO ``bedrock_cache`` key, and Bedrock
      REJECTS a top-level ``cache_control`` outright ("Extra inputs are not permitted").
      LangChain shipped precisely this regression: their Anthropic prompt-caching middleware
      broke on Bedrock in 1.4.1 by switching to top-level automatic caching (langchain#37042).

    Breakpoint budget is safe on both. pydantic-ai's ``_limit_cache_points`` drops the Anthropic
    budget to ``MAX_CACHE_POINTS = 3`` when automatic caching is on, and this sets exactly 3
    (two explicit + the server-applied one); Bedrock's limit is 4 and this sets 3.

    SINGLE-TURN CALLS ARE BYTE-IDENTICAL to the pre-dd27 behavior — there is no history to
    cache, so no third breakpoint is added and the emitted mapping is unchanged."""
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
