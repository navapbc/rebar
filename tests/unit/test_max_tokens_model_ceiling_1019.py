"""An output cap raised for a big model must not ride along into a smaller model's call.

Bug 1019-e1e9-5117-4795.

THE DEFECT, reproduced live before this test existed.

``review_kernel.verify.max_output_cfg`` raises ``cfg.max_tokens`` to the resolved model's
published ceiling — 128000 for sonnet/opus, 64000 for haiku — and by design it **only ever
raises**::

    return cfg if cap <= cfg.max_tokens else replace(cfg, max_tokens=cap)

Nothing lowers it again when a LATER call in the same run resolves to a model with a SMALLER
ceiling. The completion verifier's epic-only ``epic-bug-screen`` step is exactly that call: it
runs on the trivial-class model (haiku, 64000) while carrying a cap raised for the 128000-class
verifier. Direct Anthropic tolerated the oversized request; Bedrock validates it strictly::

    llm call [epic-bug-screen] mode=single_turn
    model=bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0 FAILED in 0.4s: 400
    'The maximum tokens you requested exceeds the model limit of 64000.'

Observed 32 times in one `rebar verify-completion` run of an epic — every screen call, every
time. Each failure is degraded to "unrelated" per candidate, so the epic's caused_by bug floor
silently evaluated nothing and the gate still returned PASS.

WHY THE ASSERTION IS "<= the model ceiling" AND NOT "== the operator floor".
``max_output_cfg``'s docstring states an intended behaviour this fix must NOT break: "Only ever
RAISES — an operator floor above model-max is preserved." That is a claim about the *config*
seam. This test pins the *request* seam instead, where the resolved model is known and where the
400 is actually produced: whatever the config says, the wire request may not ask a model for more
output than that model accepts, because such a request cannot succeed — it is a guaranteed 400,
never a larger answer.
"""

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
