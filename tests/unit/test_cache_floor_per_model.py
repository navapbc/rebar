"""Cache warnings use the model's floor and the marked prefix.

The published minimums at
https://platform.claude.com/docs/en/build-with-claude/prompt-caching are:

* claude-opus-5, fable-5, mythos-5: 512 tokens
* claude-opus-4-8, sonnet-5, sonnet-4-6, sonnet-4-5: 1024 tokens
* claude-opus-4-7: 2048 tokens
* claude-opus-4-6, claude-opus-4-5, claude-haiku-4-5: 4096 tokens

Unknown models keep a conservative fallback. A large total input can still
have an uncacheable marked prefix, so warnings name that prefix rather than
the total. Tests guard both missed cache failures and false warnings for
models with higher floors."""

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
    """A large unmarked user message cannot make a tiny marked prefix cacheable.
    Name the marked prefix and its floor so the warning identifies the remedy."""
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
