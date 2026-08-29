"""``_intersect_capabilities`` — cache capability follows the SERVING (primary) provider.

Regression coverage for bug ``96f3-af59-ba26-4159`` (prompt caching silently disabled since
2026-08-12). ``_intersect_capabilities`` used to collapse ``prompt_cache_style`` to ``"none"``
whenever a fallback chain's candidates disagreed, on the premise that provider-specific cache
keys "would error on the candidate that does not share it". That premise is FALSE: the cache
directive is a set of provider-scoped ``ModelSettings`` keys (``bedrock_cache_*`` vs
``anthropic_cache_*``); ``pydantic_ai.settings.merge_model_settings`` is a plain dict union with
no key validation, and each provider model reads ONLY its own ``*_cache_*`` keys via
``model_settings.get(...)`` — so a foreign key is a silent ``.get()`` miss, never an error
(mirrors LiteLLM: an inapplicable/below-threshold cache directive is "silently skipped … no
error is returned").

Therefore cache capability is a property of the RESOLVED serving model and must follow the
PRIMARY candidate (which serves ~99.6% of calls per ``.rebar/usage.jsonl``); a rare fallback
runs uncached-but-correct. Fields that CAN hard-fail cross-provider (``supports_temperature`` →
HTTP 400, native-output routing, thinking, web provenance) stay conservatively intersected.

Authoritative contract: operator decision on ticket 96f3-af59 (reverses the documented intent in
``rebar.toml`` [llm.model_classes] and ADR 0059 §7), grounded in the proven ignored-not-errored
behavior of the pinned pydantic-ai.
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
    """THE soundness proof for option (a): when the rare Anthropic fallback ACTUALLY serves, the
    Bedrock-primary cache directive it receives must be a no-op, NOT a provider rejection.

    This is the exact hazard the old collapse-to-none claimed to prevent. It refutes that premise
    at the seam: the chain (Bedrock primary) yields ``"bedrock"``, ``cache_settings_for`` emits
    ONLY ``bedrock_cache_*`` keys, and merging them into the Anthropic arm's settings (the plain
    dict union pydantic-ai performs per candidate) leaves every ``anthropic_cache_*`` read — the
    only keys ``AnthropicModel`` consults — as ``None``. So the fallback applies no caching, adds
    no top-level ``cache_control``, and cannot reach the ``anthropic_cache`` /
    ``anthropic_cache_messages`` mutual-exclusion ``UserError``: uncached-but-correct, no error."""
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
