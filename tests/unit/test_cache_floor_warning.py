"""Zero cache counters warn only for a cacheable prefix.

Below the shared floor, zero reads and writes are expected and stay silent.
At or above the floor they signal ineffective caching. The warning and
Pass 1 warm-up share one floor definition."""

from __future__ import annotations

import logging

import pytest

# The floor as the PRE-EXISTING consumer sees it (the Pass-1 warm-up decision). Importing it
# from there rather than restating 4096 is deliberate: if the warning ever grew its own second
# literal and the two drifted, the boundary tests below would fail.
from rebar.llm.plan_review.pass1 import CACHE_MIN_PREFIX_TOKENS as FLOOR
from rebar.llm.structured_run import warn_if_cache_ineffective

_ZERO_COUNTERS = {"cache_read_tokens": 0, "cache_write_tokens": 0}


def _usage(input_tokens: int) -> dict[str, int]:
    """A healthy, real, BILLED call that reports no cache effect whatsoever."""
    return {"input_tokens": input_tokens, "output_tokens": 5, **_ZERO_COUNTERS}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# These observed per-call input sizes fall below the floor. FLOOR - 1 also
# checks the boundary where zero cache counters must stay silent.
@pytest.mark.parametrize("input_tokens", [1822, 2184, 2317, 3140, FLOOR - 1])
def test_no_warning_below_the_cacheable_prefix_floor(caplog, input_tokens):
    """Below the cacheable-prefix floor, zero cache counters are expected. Do not
    warn about a prompt too short to cache."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(input_tokens),
            caching_requested=True,
            model="bedrock:us.anthropic.claude-sonnet-4-6",
        )
    assert _warnings(caplog) == [], (
        f"input_tokens={input_tokens} is below the {FLOOR}-token cacheable-prefix floor, so "
        "zero cache counters are the EXPECTED reading, not a defect to warn about"
    )


# At the floor caching COULD have engaged; above it, comfortably so. 4096 is the boundary
# itself (>=, not >), and 8192/40290 stand in for the genuine measured defect.
@pytest.mark.parametrize("input_tokens", [FLOOR, FLOOR + 1, 8192, 40290])
def test_still_warns_at_or_above_the_floor_when_both_counters_are_zero(caplog, input_tokens):
    """At or above the floor, zero read and write counters still warn about a
    cacheable prompt that did not cache."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(input_tokens),
            caching_requested=True,
            model="us.anthropic.claude-opus-4-5",
        )
    warnings = _warnings(caplog)
    assert len(warnings) == 1, (
        f"input_tokens={input_tokens} clears the {FLOOR}-token floor, so zero cache counters "
        "mean caching silently failed on a cacheable prompt — the signal must survive"
    )
    assert "us.anthropic.claude-opus-4-5" in warnings[0], (
        "the warning must name the model so the operator can switch to a caching one"
    )


def test_floor_gate_does_not_swallow_the_other_no_warning_reasons(caplog):
    """The floor is an ADDITIONAL bound, not a replacement: an above-floor call that DID cache,
    and one that never requested caching, stay silent as before."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            {"input_tokens": 13, "cache_read_tokens": 40170, "cache_write_tokens": 0},
            caching_requested=True,
            model="us.anthropic.claude-opus-4-8",
        )
        warn_if_cache_ineffective(_usage(40290), caching_requested=False, model="openai:gpt-4o")
    assert _warnings(caplog) == []


def test_the_floor_has_a_single_shared_definition():
    """The warning and Pass 1 warm-up read the same floor constant from
    capabilities.py."""
    from rebar.llm import capabilities
    from rebar.llm.plan_review import pass1

    assert capabilities.CACHE_MIN_PREFIX_TOKENS == 4096
    assert pass1.CACHE_MIN_PREFIX_TOKENS == capabilities.CACHE_MIN_PREFIX_TOKENS
