"""Bug 2ca9: usage-log pricing silently dropped provider-qualified rows.

Epic 061c made model strings provider-qualified and ``agent_call.py`` records
``model=ran_model`` verbatim, so a stored row's model became ``anthropic:claude-...``.
``_price_row`` passed that STORED string through as genai-prices' ``model_ref``, which
resolves a BARE id — so every Anthropic and OpenAI row raised ``LookupError`` (the
unknown-model signal), got caught, and became "unpriced" with no warning. Only the
``bedrock:`` form happened to resolve, which biased the epic's own provider cost
comparison in the wrong direction: Bedrock was the only arm being measured at all.

These tests run against the REAL genai-prices (the ``pricing`` extra, present in the dev
env via pydantic-ai) because the defect IS the real library's resolution behaviour — a
stub that accepts whatever it is handed cannot see it.
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib
import sys
import types

import pytest

from rebar.llm import usage_log

#: Realistic stored rows, one per provider the epic runs, in exactly the shape
#: ``agent_call.record()`` writes: a provider-qualified ``model`` plus a bare ``provider``.
QUALIFIED_ROWS = [
    ("anthropic", "anthropic:claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("openai", "openai:gpt-5.4", "gpt-5.4"),
    ("bedrock", "bedrock:us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"),
]


def _row(provider: str, model: str) -> dict:
    return {
        "op": "review",
        "model": model,
        "provider": provider,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "requests": 1,
        "timestamp": "2026-07-30T00:00:00+00:00",
    }


@pytest.mark.parametrize(("provider", "qualified", "bare"), QUALIFIED_ROWS)
def test_provider_qualified_row_prices_for_every_affected_provider(provider, qualified, bare):
    """AC1 + AC2 + AC3. A stored provider-qualified row must price, for ALL THREE providers —
    a fix that only handled the ``anthropic:`` prefix would leave OpenAI blank and the
    comparison bias intact. Pinned against the price of the BARE id, so this asserts the
    translation rather than a hard-coded dollar figure that pricing data would drift out of."""
    pricing = pytest.importorskip("genai_prices")

    expected = float(
        pricing.calc_price(
            pricing.Usage(input_tokens=1000, output_tokens=500),
            model_ref=bare,
            provider_id=provider,
        ).total_price
    )
    assert expected > 0, f"precondition: genai-prices must know the bare id {bare!r}"

    cost = usage_log._price_row(pricing, _row(provider, qualified))

    assert cost is not None, (
        f"{provider} row with stored model {qualified!r} priced as unpriced — the "
        "provider-qualified id was handed straight to genai-prices as model_ref"
    )
    assert cost == pytest.approx(expected)


def test_bedrock_stays_dearer_than_anthropic_for_the_same_model():
    """AC3's rate relationship. Bedrock resells the same Claude model at a premium (~10%),
    so once both arms are actually measured, Bedrock must read HIGHER than direct Anthropic.
    Before the fix only Bedrock priced at all, which read as "Bedrock is the expensive arm"
    when it was the only arm with a number. Asserting the direction and the magnitude band
    keeps that asymmetry from silently returning."""
    pricing = pytest.importorskip("genai_prices")

    anthropic = usage_log._price_row(pricing, _row("anthropic", "anthropic:claude-sonnet-4-6"))
    bedrock = usage_log._price_row(
        pricing, _row("bedrock", "bedrock:us.anthropic.claude-sonnet-4-6")
    )

    assert anthropic is not None and bedrock is not None
    assert bedrock > anthropic, "the same model via Bedrock must not read cheaper than direct"
    assert bedrock / anthropic < 1.5, (
        "Bedrock's premium on the same model is a resale margin, not a different order of "
        f"magnitude — {bedrock} vs {anthropic} means one arm is priced against the wrong model"
    )


def test_translation_is_registry_membership_not_a_colon_split():
    """AC4. The qualifier is dropped by MEMBERSHIP in ``config.KNOWN_PROVIDER_NAMES``, never by
    splitting on ``":"``. That is a correctness requirement, not a style one: a real Bedrock id
    such as ``anthropic.claude-haiku-4-5-20251001-v1:0`` prices today, and a blind split on the
    first colon would hand genai-prices ``"0"``. An unrecognized prefix must survive intact."""
    aws_versioned_id = "anthropic.claude-haiku-4-5-20251001-v1:0"

    assert usage_log._pricing_model_ref(aws_versioned_id) == aws_versioned_id
    assert usage_log._pricing_model_ref("not-a-provider:some-model") == "not-a-provider:some-model"
    # A recognized qualifier IS dropped.
    assert usage_log._pricing_model_ref("anthropic:claude-sonnet-4-6") == "claude-sonnet-4-6"

    pricing = pytest.importorskip("genai_prices")
    priced = usage_log._price_row(pricing, _row("bedrock", aws_versioned_id))
    assert priced is not None and priced > 0, (
        "the versioned AWS id must still price — this is the id a colon-split would destroy"
    )


def test_usage_log_does_not_prefix_match_or_colon_split_provider_names():
    """AC4's structural pin, mirroring ``capabilities.py``'s attested no-prefix-matching guard
    (``test_capabilities_module_still_has_no_provider_name_prefix_matching``). Asserted on real
    calls, not on text, so a comment naming the banned pattern stays legal."""
    tree = ast.parse(pathlib.Path(usage_log.__file__).read_text())
    banned = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"startswith", "endswith", "split", "partition", "removeprefix"}
        and any(isinstance(a, ast.Constant) and a.value == ":" for a in node.args)
    ]
    assert not banned, (
        "usage_log.py must decide the pricing model_ref by registry membership "
        f"(config.split_provider_qualifier), not by parsing ':' itself: "
        f"{[node.lineno for node in banned]}"
    )
    # No provider name is hard-coded either — the registry is the single source.
    provider_literals = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in {"anthropic", "openai", "bedrock"}
    ]
    assert not provider_literals, (
        f"provider names must come from config.KNOWN_PROVIDER_NAMES: {provider_literals}"
    )


def _stub_pricing(monkeypatch, calc_price):
    stub = types.ModuleType("genai_prices")

    class Usage:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    stub.Usage = Usage
    stub.calc_price = calc_price
    monkeypatch.setitem(sys.modules, "genai_prices", stub)
    return stub


def test_unknown_model_is_tolerated_and_named_so_it_is_distinguishable(
    tmp_path, monkeypatch, caplog
):
    """AC5. A model genuinely unknown to genai-prices stays tolerated as unpriced — pricing is
    best-effort and must never break ``summarize`` — but the footer now NAMES each id that
    failed to resolve, and it names the id that was actually looked up. That is what makes the
    two causes distinguishable: a genuine unknown appears BARE (``mystery-model``), while a row
    dropped because its id is mis-formatted appears with the malformation still visible
    (``not-a-provider:m``). Before this, both read only as "excludes N unpriced calls"."""

    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        if model_ref in {"mystery-model", "not-a-provider:m"}:
            raise LookupError(f"Unable to find model with model_ref={model_ref!r}")

        class _P:
            total_price = 0.01

        return _P()

    _stub_pricing(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                _row("anthropic", "anthropic:claude-sonnet-4-6"),
                _row("anthropic", "mystery-model"),
                _row("anthropic", "not-a-provider:m"),
            )
        )
        + "\n"
    )

    with caplog.at_level(logging.WARNING, logger="rebar.llm.usage_log"):
        out = usage_log.summarize(str(path))

    # Tolerated: the summary still renders, and the priceable row still contributes.
    assert "excludes 2 unpriced calls" in out
    assert "$0.0100" in out
    # LookupError stays the expected unknown-model signal, not an error.
    assert not caplog.records
    # Distinguishable: both ids are named, and the mis-formatted one shows its malformation.
    assert "mystery-model" in out
    assert "not-a-provider:m" in out
    # The row that DID price is not slandered as unpriced.
    unpriced_line = next(line for line in out.splitlines() if "unpriced call" in line)
    assert "claude-sonnet-4-6" not in unpriced_line


def test_a_row_without_a_model_is_not_named_as_an_unknown_model(tmp_path, monkeypatch):
    """Pre-pricing rows carry no model at all. They are unpriced, but there is no id to blame,
    so the footer must not invent one."""

    def calc_price(usage, model_ref, provider_id=None, genai_request_timestamp=None):
        raise AssertionError("must not be called for a row with no model")

    _stub_pricing(monkeypatch, calc_price)
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"op": "a", "input_tokens": 10, "requests": 1}) + "\n")

    out = usage_log.summarize(str(path))

    assert "excludes 1 unpriced call" in out
    assert "No pricing data for" not in out
