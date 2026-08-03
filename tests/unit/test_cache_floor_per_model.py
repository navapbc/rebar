"""The cacheable-prefix floor is MODEL-dependent, and the warning must measure the MARKED
PREFIX against it — not the total ``input_tokens`` (bug e3cd).

Two separable defects, both pinned here.

DEFECT 1 — the floor was a model-blind global. ``CACHE_MIN_PREFIX_TOKENS = 4096`` was
commented as the Opus floor and applied to every model. Anthropic publishes a per-model
minimum and 4096 is only correct for a subset of them:

    https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    (§ "Minimum cacheable prompt length", read 2026-08-02)

        claude-opus-5 / fable-5 / mythos-5                            512
        claude-opus-4-8 / sonnet-5 / sonnet-4-6 / sonnet-4-5          1024
        claude-opus-4-7                                               2048
        claude-opus-4-6 / claude-opus-4-5 / claude-haiku-4-5          4096

The published numbers are CONFIRMED by two independent empirical brackets recorded on the
ticket (they VERIFY the documented values; they do not define them):

        sonnet-4-6    922 -> no cache, 1042 -> write 1035/read 1035   => ~1024  ✓ doc 1024
        sonnet-4-5    253 -> no cache, 7930 -> cached                 => <=1024 ✓ doc 1024
        haiku-4-5    2748 -> no cache, 4749 -> write 4742/read 4742   => (2748,4749] ✓ doc 4096

Note what that costs today: ``claude-opus-4-8`` is rebar's DEFAULT_MODEL and its real floor is
1024, so the global constant is FOUR TIMES too high on the model rebar runs most.

DEFECT 2 — the predicate compared the wrong quantity. Caching is governed by the size of the
MARKED PREFIX (the bytes ahead of the ``cache_control`` breakpoint), not by the total billed
input. A call with a 150-token marked prefix behind a 7000-token unmarked user message clears
any floor on totals and is uncacheable in fact.

The contract these tests pin:

* the floor is carried per-model on the capability record, at the documented values
* an unknown/unlisted model keeps the conservative global (never invents a number)
* the warning measures the MARKED PREFIX against that model's floor
* a marked prefix that clears a LOW floor but not the old global now warns (the false
  negative bug e3cd names)
* the haiku floor is NOT lowered to sonnet's (does not reintroduce the over-warning of 7a79)
* a large-total / small-marked-prefix call warns and NAMES the sub-floor marked prefix as the
  cause, instead of reporting the total and implying the wrong remedy
"""

from __future__ import annotations

import logging

import pytest

from rebar.llm.capabilities import CACHE_MIN_PREFIX_TOKENS, capabilities_for
from rebar.llm.structured_run import warn_if_cache_ineffective

_ZERO_COUNTERS = {"cache_read_tokens": 0, "cache_write_tokens": 0}


def _usage(input_tokens: int) -> dict[str, int]:
    """A healthy, real, BILLED call that reports no cache effect whatsoever."""
    return {"input_tokens": input_tokens, "output_tokens": 5, **_ZERO_COUNTERS}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ── Defect 1: the floor is per-model, at the DOCUMENTED values ────────────────────────


@pytest.mark.parametrize(
    ("model_string", "documented_floor"),
    [
        # Every value below is transcribed from Anthropic's published per-model table (cited
        # in the module docstring), NOT inferred from the empirical brackets.
        ("anthropic:claude-opus-5", 512),
        ("anthropic:claude-opus-4-8", 1024),  # rebar's DEFAULT_MODEL
        ("anthropic:claude-sonnet-4-6", 1024),
        ("anthropic:claude-sonnet-4-5", 1024),
        ("anthropic:claude-opus-4-7", 2048),
        ("anthropic:claude-opus-4-6", 4096),
        ("anthropic:claude-haiku-4-5", 4096),
    ],
)
def test_cache_floor_is_carried_per_model_at_the_documented_value(model_string, documented_floor):
    """The floor rides on the capability record alongside ``prompt_cache_style``.

    Asserting the exact documented integer (not merely "some int", and not merely "less than
    the global") is deliberate: a fix that lowered every model to one NEW global would satisfy
    a looser assertion while reproducing the defect."""
    caps = capabilities_for(model_string)
    assert caps.cache_min_prefix_tokens == documented_floor


def test_the_floors_actually_differ_between_models():
    """The whole defect is that ONE constant cannot express a per-model fact.

    A fix that made the record model-aware but returned the same number everywhere would pass
    every single-model assertion above; this is the test that catches it."""
    sonnet = capabilities_for("anthropic:claude-sonnet-4-6").cache_min_prefix_tokens
    haiku = capabilities_for("anthropic:claude-haiku-4-5").cache_min_prefix_tokens
    opus5 = capabilities_for("anthropic:claude-opus-5").cache_min_prefix_tokens
    assert sonnet < haiku, "sonnet's floor must be lower than haiku's"
    assert opus5 < sonnet, "opus-5's floor must be lower than sonnet's"


def test_unlisted_model_keeps_the_conservative_global_rather_than_inventing_one():
    """An unmeasured/undocumented model must NOT be assigned a guessed floor.

    The conservative direction is the HIGH one: too high under-warns (a missed signal),
    too low re-creates 7a79's unactionable warning spam."""
    caps = capabilities_for("anthropic:claude-some-unreleased-model")
    assert caps.cache_min_prefix_tokens == CACHE_MIN_PREFIX_TOKENS


def test_non_caching_provider_still_reports_a_floor_and_never_none():
    """`cache_min_prefix_tokens` must always be an int so callers need no None-handling."""
    caps = capabilities_for("openai:gpt-4o")
    assert caps.prompt_cache_style == "none"
    assert isinstance(caps.cache_min_prefix_tokens, int)


# ── Defect 2: the warning measures the MARKED PREFIX against that model's floor ───────


def test_sub_4096_but_above_sonnet_floor_marked_prefix_now_warns(caplog):
    """e3cd's FALSE NEGATIVE, directly.

    A 1500-token marked prefix on sonnet is comfortably above sonnet's real 1024 floor, so it
    WAS cacheable and reporting zero/zero is a genuine silent failure. Under the old
    model-blind 4096 global this was silently swallowed."""
    assert 1024 < 1500 < CACHE_MIN_PREFIX_TOKENS, "the test value must sit in the blind spot"
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(1800),
            caching_requested=True,
            model="anthropic:claude-sonnet-4-6",
            marked_prefix_tokens=1500,
            cache_min_prefix_tokens=1024,
        )
    assert _warnings(caplog), "a cacheable 1500-token prefix reporting zero/zero must warn"


def test_haiku_floor_is_not_lowered_to_sonnets(caplog):
    """The fix must not reintroduce the over-warning bug 7a79 removed.

    The IDENTICAL 1500-token marked prefix that warns on sonnet above must stay SILENT on
    haiku, whose documented floor is 4096: below it the cache never writes or reads, so
    zero/zero is the CORRECT reading and no configuration change could alter it."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(1800),
            caching_requested=True,
            model="anthropic:claude-haiku-4-5",
            marked_prefix_tokens=1500,
            cache_min_prefix_tokens=4096,
        )
    assert not _warnings(caplog), "a sub-floor prefix on a high-floor model must stay silent"


def test_large_total_with_tiny_marked_prefix_warns_and_names_the_marked_prefix(caplog):
    """The two MEASURED production calls (bug f81d), in the shape they actually have.

    coach_notes shipped system[0] ~150 tok behind an UNMARKED ~6928-token user message. The old
    predicate compared the 7078 TOTAL against 4096, cleared it, and reported "input_tokens=7078"
    — true that caching failed, but it named the wrong quantity and so implied the wrong remedy
    (the prompt looks plenty big). The warning must instead name the 150-token marked prefix and
    the floor it fell under, because THAT is the changeable thing."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(7078),
            caching_requested=True,
            model="anthropic:claude-sonnet-4-6",
            marked_prefix_tokens=150,
            cache_min_prefix_tokens=1024,
        )
    messages = _warnings(caplog)
    assert messages, "a 7078-token call paying full price must not go silent"
    joined = " ".join(messages)
    assert "150" in joined, "the warning must name the MARKED PREFIX size"
    assert "1024" in joined, "the warning must name the floor it fell under"


def test_tiny_call_with_tiny_marked_prefix_stays_silent(caplog):
    """A genuinely small call has no bleed to recover, so it must not warn.

    This is the bound that keeps the cause-naming warning from re-creating 7a79's spam: the
    signal is "a large payload is riding outside the breakpoint", not "the prefix is small"."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(300),
            caching_requested=True,
            model="anthropic:claude-sonnet-4-6",
            marked_prefix_tokens=150,
            cache_min_prefix_tokens=1024,
        )
    assert not _warnings(caplog)


def test_caching_not_requested_is_always_silent(caplog):
    """Unchanged contract: no claim can be made about a call that never asked to cache."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(70000),
            caching_requested=False,
            model="openai:gpt-4o",
            marked_prefix_tokens=10,
            cache_min_prefix_tokens=1024,
        )
    assert not _warnings(caplog)


def test_nonzero_cache_counters_are_always_silent(caplog):
    """Unchanged contract: caching demonstrably worked, so there is nothing to report."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            {"input_tokens": 7078, "cache_read_tokens": 6900, "cache_write_tokens": 0},
            caching_requested=True,
            model="anthropic:claude-sonnet-4-6",
            marked_prefix_tokens=150,
            cache_min_prefix_tokens=1024,
        )
    assert not _warnings(caplog)


def test_unknown_marked_prefix_falls_back_to_the_pre_existing_total_predicate(caplog):
    """A caller that cannot measure its marked prefix keeps today's exact behavior.

    This is what keeps every pre-existing call site (and bug 7a79's floor semantics) intact
    while the marked-prefix measurement is threaded through."""
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(7078), caching_requested=True, model="anthropic:claude-sonnet-4-6"
        )
    assert _warnings(caplog), "above the default floor on totals, the old predicate still fires"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            _usage(1822), caching_requested=True, model="anthropic:claude-sonnet-4-6"
        )
    assert not _warnings(caplog), "below the default floor on totals, still silent (7a79)"
