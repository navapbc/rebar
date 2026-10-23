"""Prompt caching follows the serving primary provider. Provider-specific cache
settings are ignored by another provider, so a fallback can run uncached. In
contrast, temperature, native output, thinking, and web-provenance settings can
fail across providers and remain conservatively intersected.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.runner_support import _intersect_capabilities

pytestmark = pytest.mark.unit


def _caps(style: str, **overrides) -> ModelCapabilities:
    """A capability record with a given ``prompt_cache_style`` and sensible defaults."""
    fields: dict = {
        "native_structured_output": True,
        "prompt_cache_style": style,
        "supports_thinking": True,
        "supports_temperature": True,
        "native_web_search": True,
        "native_output_with_thinking": True,
    }
    fields.update(overrides)
    return ModelCapabilities(**fields)


def test_mixed_chain_keeps_primary_bedrock_cache_style():
    """The core regression: a mixed Bedrock-primary + Anthropic-fallback chain must keep the
    PRIMARY's ``"bedrock"`` cache style, not collapse to ``"none"``. This is what restores
    prompt caching on the Bedrock target that serves virtually every call."""
    primary = _caps("bedrock")
    fallback = _caps("anthropic")
    chain = _intersect_capabilities([primary, fallback])
    assert chain.prompt_cache_style == "bedrock"


def test_mixed_chain_anthropic_primary_keeps_anthropic_cache_style():
    """Symmetry: whichever provider is PRIMARY dictates the chain's cache style — a chain led by
    a direct-Anthropic primary keeps ``"anthropic"`` even with a Bedrock fallback."""
    chain = _intersect_capabilities([_caps("anthropic"), _caps("bedrock")])
    assert chain.prompt_cache_style == "anthropic"


def test_primary_uncacheable_stays_none_even_with_cacheable_fallback():
    """A chain whose PRIMARY cannot cache (style ``"none"``) stays ``"none"`` regardless of a
    cacheable fallback: the primary serves, so there is no cache directive to emit for it."""
    chain = _intersect_capabilities([_caps("none"), _caps("bedrock")])
    assert chain.prompt_cache_style == "none"


def test_same_style_chain_unchanged():
    """Negative control: a homogeneous chain still reports that shared style (behavior the fix
    must not change)."""
    assert _intersect_capabilities([_caps("bedrock"), _caps("bedrock")]).prompt_cache_style == (
        "bedrock"
    )


def test_hard_fail_fields_stay_conservatively_intersected_on_mixed_chain():
    """Guard against over-broadening the fix to "primary wins for everything". Fields that can
    hard-fail across providers MUST remain ``all(...)`` intersections even though cache now
    follows the primary — a primary that supports temperature paired with a fallback measured to
    reject it must still withdraw temperature for the whole chain (any candidate may answer)."""
    primary = _caps("bedrock", supports_temperature=True, native_structured_output=True)
    fallback = _caps("anthropic", supports_temperature=False, native_structured_output=False)
    chain = _intersect_capabilities([primary, fallback])
    # cache follows the primary ...
    assert chain.prompt_cache_style == "bedrock"
    # ... but the erroring-capable fields stay conservative.
    assert chain.supports_temperature is False
    assert chain.native_structured_output is False


@pytest.mark.parametrize("execution_mode", ["single", "agentic"])
def test_fallback_fires_primary_cache_directive_is_inert_on_anthropic_arm(execution_mode):
    """A Bedrock-primary cache directive is inert on an Anthropic fallback. The
    settings merge retains bedrock_cache keys, while Anthropic reads only
    anthropic_cache keys. The fallback therefore runs uncached without a provider
    rejection or mutually exclusive cache settings.
    """
    from pydantic_ai.models.anthropic import AnthropicModelSettings
    from pydantic_ai.settings import merge_model_settings

    from rebar.llm.capabilities import cache_settings_for

    chain = _intersect_capabilities([_caps("bedrock"), _caps("anthropic")])
    directive = cache_settings_for(chain, execution_mode=execution_mode)
    assert directive is not None
    assert all(k.startswith("bedrock_cache") for k in directive)

    # Anthropic arm serves the fallback and merges the run-level (bedrock) directive in.
    merged = merge_model_settings(AnthropicModelSettings(), directive)
    anthropic_cache_reads = (
        "anthropic_cache_instructions",
        "anthropic_cache_tool_definitions",
        "anthropic_cache",
        "anthropic_cache_messages",
    )
    assert all(merged.get(k) is None for k in anthropic_cache_reads)
    # The mutual-exclusion UserError (anthropic.py) is unreachable: never both set.
    assert not (merged.get("anthropic_cache") and merged.get("anthropic_cache_messages"))
