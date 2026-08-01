"""The cache-ineffective warning must respect the cacheable-prefix FLOOR (bug 7a79).

``warn_if_cache_ineffective`` (story S3/2932) exists to catch a REAL, silent defect: a model
that reports ``cache_read_tokens == 0`` AND ``cache_write_tokens == 0`` while billing the full
input, with no provider error — so the operator pays full price forever with no signal. That
detection is valuable and these tests keep it.

What it could NOT previously tell apart is a model that *fails* to cache a cacheable prompt
from a prompt that was never cacheable in the first place. The anthropic cache will not
write/read a prefix below ``CACHE_MIN_PREFIX_TOKENS`` (4096) — a fact the codebase already
encoded for the Pass-1 warm-up decision — so below that floor zero/zero is the EXPECTED
reading, not a symptom. The predicate had no lower bound, so it fired on every small call
(~20 lines per ``rebar review-plan`` run), and the run's own aggregate usage showed
``cache_write_tokens > 0`` at the same time the per-call lines claimed caching "had NO effect".

The contract these tests pin:

* below the floor + zero counters -> SILENT (nothing actionable exists to report)
* at/above the floor + zero counters -> WARN (the genuine silent-failure signal, kept)
* the floor has ONE definition, shared by the warning and the Pass-1 warm-up decision
"""

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


# The exact per-call ``input_tokens`` values OBSERVED warning during a live ``review-plan`` run
# (ticket 7a79), across the direct-Anthropic and Bedrock paths. Every one is under the floor, so
# every one was a false alarm; ``FLOOR - 1`` pins the boundary itself.
@pytest.mark.parametrize("input_tokens", [1822, 2184, 2317, 3140, FLOOR - 1])
def test_no_warning_below_the_cacheable_prefix_floor(caplog, input_tokens):
    """Below the floor the anthropic cache never writes or reads, so zero/zero is CORRECT.

    Warning here is not merely noisy, it is unactionable: no configuration change could make a
    sub-floor prompt cache. Firing ~20 times per gate run trains the operator to filter out the
    one signal that catches genuine silent cost bleed."""
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
    """The genuine defect the warning exists for MUST stay caught.

    MEASURED: opus-4-5 bills the full input on every call with cache_read=0 AND cache_write=0
    and no provider error. Above the floor the prompt WAS cacheable and demonstrably did not
    cache, which is exactly the silent cost bleed an operator can act on (switch models /
    providers) and would otherwise never see."""
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
    """AC: ONE definition of the floor, not a second literal.

    The warning (``llm/structured_run.py``) and the Pass-1 warm-up decision
    (``llm/plan_review/pass1.py``) must read the same constant from the same shared home.
    ``capabilities.py`` is that home — the lowest layer both already sit above, and the module
    that already owns the prompt-cache knowledge (``cache_settings_for``)."""
    from rebar.llm import capabilities
    from rebar.llm.plan_review import pass1

    assert capabilities.CACHE_MIN_PREFIX_TOKENS == 4096
    assert pass1.CACHE_MIN_PREFIX_TOKENS == capabilities.CACHE_MIN_PREFIX_TOKENS
