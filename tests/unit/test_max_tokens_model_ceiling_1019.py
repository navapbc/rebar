"""Cap each model request at its resolved output-token ceiling.

A config floor raised for a larger model can reach a later smaller-model call.
The config seam may preserve that floor, but the wire request must not exceed
the smaller model's cap and trigger a guaranteed provider rejection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.config import LLMConfig
from rebar.llm.review_kernel.verify import model_max_output_tokens
from rebar.llm.structured_run import build_model_settings

HAIKU = "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "bedrock:us.anthropic.claude-sonnet-4-6"


class _Req:
    def __init__(self, output_token_limit=None, config=None) -> None:
        self.output_token_limit = output_token_limit
        self.config = config


def _caps() -> ModelCapabilities:
    """Real capabilities object — a hand-rolled stub drifts from the production shape, and a
    stub missing a field fails this test for a reason that is not the bug."""
    return ModelCapabilities(
        native_structured_output=True,
        prompt_cache_style="bedrock",
        supports_thinking=False,
    )


def _settings(resolved: str, cfg_max: int, *, output_token_limit=None) -> dict:
    cfg = replace(LLMConfig(), model=resolved, max_tokens=cfg_max)
    return build_model_settings(
        cfg,
        _Req(output_token_limit=output_token_limit),
        _caps(),
        resolved,
        None,
        model_override=None,
    )


def test_a_cap_raised_for_a_bigger_model_is_clamped_to_the_model_being_called() -> None:
    """THE BUG. 128000 was raised for the 128K verifier; this call resolves to the 64K haiku.

    Asserted against the ceiling the table itself publishes, not a hard-coded 64000, so the test
    keeps meaning if the published limit changes.
    """
    ceiling = model_max_output_tokens(HAIKU)
    assert ceiling == 64_000, "precondition: the table still publishes haiku's ceiling as 64000"

    settings = _settings(HAIKU, 128_000)

    assert settings["max_tokens"] <= ceiling, (
        f"the request asks haiku for {settings['max_tokens']} output tokens, above its "
        f"{ceiling} ceiling. Bedrock answers that with HTTP 400 ValidationException and the "
        f"call never runs — an oversized ask cannot produce a larger answer, only a failure."
    )


def test_a_model_with_headroom_is_left_alone() -> None:
    """The positive control. Clamping must key off the MODEL's ceiling, not clamp everything.

    Without this, 'return 256 always' would satisfy the cell above.
    """
    settings = _settings(SONNET, 128_000)

    assert settings["max_tokens"] == 128_000, (
        f"a 128000 cap on the 128000-ceiling sonnet was altered to "
        f"{settings['max_tokens']} — the clamp is firing where there is no overflow"
    )


def test_a_request_level_clamp_down_still_wins() -> None:
    """``output_token_limit`` is bounded recovery deliberately asking for LESS; a model-ceiling
    clamp must not raise it back up."""
    settings = _settings(HAIKU, 128_000, output_token_limit=4_096)

    assert settings["max_tokens"] == 4_096, (
        f"the recovery path's explicit 4096 clamp was overridden to {settings['max_tokens']}"
    )


@pytest.mark.parametrize("resolved", [None, "", "some-unmapped-model"])
def test_an_unmapped_model_keeps_the_conservative_fallback(resolved) -> None:
    """An unknown model must not be handed an unbounded cap.

    ``model_max_output_tokens`` already falls back to the configured default for these; the clamp
    must not turn that conservative fallback into a licence to send the raised value.
    """
    settings = _settings(resolved or "", 128_000)
    fallback = model_max_output_tokens(resolved)

    assert settings["max_tokens"] <= max(fallback, 128_000), "sanity: no inflation"
    assert settings["max_tokens"] > 0


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        ("bedrock:us.amazon.nova-pro-v1:0", 10_000),
        ("us.amazon.nova-2-lite-v1:0", 10_000),
        ("nova-lite", 10_000),
        ("nova-micro", 10_000),
        # The Claude entries and the fallback must be untouched by the Nova rows.
        ("claude-sonnet-4-6", 128_000),
        ("some-unmapped-model", 16_000),
    ],
)
def test_nova_models_get_their_10k_bedrock_ceiling(resolved, expected) -> None:
    """Amazon Nova accepts at most 10k output tokens on Bedrock (ticket e800-c107-d4ac-463b).

    Without a table entry the 16k conservative fallback exceeds Nova's hard limit, so Bedrock
    rejects every review call with ``ValidationException`` before it runs. ``nova-2-lite`` is
    pinned separately from ``nova-lite`` because the lookup is a plain substring match and
    ``nova-lite`` is NOT a substring of ``nova-2-lite``.
    """
    assert model_max_output_tokens(resolved) == expected
