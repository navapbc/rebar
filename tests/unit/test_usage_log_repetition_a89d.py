"""The step-budget diagnostic must separate a runaway loop from genuine breadth (a89d).

A step-budget exhaustion looks identical in the raw counters whether the agent did a lot
of legitimate work or span in a loop, and the step count provably cannot tell them apart:
``runner.py`` sets ``request_limit = ceil(max_iter/2)`` against
``tool_calls_limit = max(8, max_iter)``, so a one-tool-call-per-turn loop trips the request
ceiling first — exactly like careful sequential work does.

``run_shape`` therefore reduces each tool call to a ``tool_name:sha256(args)[:8]``
signature and summarizes the sequence. The arguments are HASHED, never recorded, so the
module's stated privacy contract (prompts, tool arguments and tool results are excluded
from the durable gate-error record) is preserved — a digest plus the tool name, which is a
fixed vocabulary, carries the signal without the content.

This mattered: on the real 9fd4 investigation the summary read
``tool_calls=475 distinct=451 max_consecutive_repeat=1``, which refuted a loop hypothesis
that the counters alone could not have settled.
"""

from __future__ import annotations

import pytest

from rebar.llm import usage_log

pytestmark = pytest.mark.unit


class _Part:
    """Stands in for a pydantic-ai ``ToolCallPart`` (matched by class NAME)."""

    def __init__(self, tool_name: str, args: object) -> None:
        self.tool_name = tool_name
        self.args = args


_Part.__name__ = "ToolCallPart"


class _Resp:
    """Stands in for a pydantic-ai ``ModelResponse`` (matched by class NAME)."""

    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts
        self.finish_reason = None
        self.usage = None


_Resp.__name__ = "ModelResponse"


def _messages(calls: list[tuple[str, object]]) -> list[object]:
    return [_Resp([_Part(name, args)]) for name, args in calls]


def _summary(calls: list[tuple[str, object]]) -> dict:
    return usage_log.run_shape(_messages(calls), request_limit=240, tool_calls_limit=480)


def test_a_loop_reads_as_one_distinct_signature_repeated() -> None:
    """The runaway shape: many calls, one signature, a long consecutive run."""
    summary = _summary([("read_file", {"path": "a.py"})] * 40)

    assert summary["tool_calls"] == 40
    assert summary["tool_calls_distinct"] == 1
    assert summary["max_consecutive_repeat"] == 40
    assert summary["top_repeated_tool_calls"][0]["count"] == 40


def test_genuine_breadth_reads_as_all_distinct_with_no_repeat() -> None:
    """The legitimate shape — the one the real 9fd4 run turned out to be."""
    summary = _summary([("search_files", {"q": f"pattern-{i}"}) for i in range(40)])

    assert summary["tool_calls"] == 40
    assert summary["tool_calls_distinct"] == 40
    assert summary["max_consecutive_repeat"] == 1
    assert summary["top_repeated_tool_calls"] == [], (
        "nothing repeated, so nothing should be reported as a repeat"
    )


def test_the_two_shapes_are_distinguishable_at_identical_call_counts() -> None:
    """The whole point: identical ``tool_calls`` must not read identically.

    Without this the diagnostic would be decorative — the counters already agreed on
    call count, and that is exactly what made the loop question unanswerable.
    """
    loop = _summary([("read_file", {"path": "a.py"})] * 40)
    breadth = _summary([("search_files", {"q": f"p{i}"}) for i in range(40)])

    assert loop["tool_calls"] == breadth["tool_calls"]
    assert loop["tool_calls_distinct"] != breadth["tool_calls_distinct"]
    assert loop["max_consecutive_repeat"] != breadth["max_consecutive_repeat"]


def test_arguments_are_hashed_not_recorded() -> None:
    """The privacy contract: a secret in tool arguments must not reach the record."""
    secret = "SUPER-SECRET-TOKEN-do-not-log"
    summary = _summary([("read_file", {"token": secret})] * 2)

    assert secret not in repr(summary), (
        "tool arguments must never appear in the durable diagnostic; only a digest"
    )
    assert summary["tool_calls_distinct"] == 1


def test_unhashable_arguments_degrade_rather_than_raise() -> None:
    """A surprising arg shape must not raise inside a failure path."""

    class _Weird:
        def __repr__(self) -> str:  # pragma: no cover - exercised via str() fallback
            return "<weird>"

    summary = _summary([("odd_tool", _Weird()), ("odd_tool", _Weird())])

    assert summary["tool_calls"] == 2
    assert summary["tool_calls_distinct"] >= 1


def test_no_tool_calls_yields_zeroed_signals() -> None:
    """A run that never called a tool reports zeros, not a missing key."""
    summary = usage_log.run_shape([], request_limit=240, tool_calls_limit=480)

    assert summary["tool_calls"] == 0
    assert summary["tool_calls_distinct"] == 0
    assert summary["max_consecutive_repeat"] == 0
    assert summary["top_repeated_tool_calls"] == []


# ---------------------------------------------------------------------------
# 70bc: the windowed distinct-ratio — the cycle-blind-spot fix.
#
# max_consecutive_repeat only sees the degenerate 1-cycle (a call repeated
# back-to-back). A k-cycle with k >= 2 has NO adjacent duplicates, so a real
# 4-call loop scores 1 while a healthy exploratory run scores 5. The windowed
# distinct-ratio (set cardinality over the trailing REPETITION_WINDOW calls,
# divided by REPETITION_WINDOW) is order-insensitive and catches every cycle
# length. Below the window the field is None: too small a sample to accuse a
# loop, so consumers can never trip on short runs.
# ---------------------------------------------------------------------------


def test_a_4_cycle_trips_the_windowed_distinct_ratio() -> None:
    """The bf31 shape: a closed 4-call cycle — invisible to
    max_consecutive_repeat (scores 1), caught by the windowed ratio."""
    summary = _summary([("read_file", {"p": i % 4}) for i in range(48)])

    assert summary["max_consecutive_repeat"] == 1  # the blind spot, pinned
    assert summary["distinct_ratio_window"] == round(4 / 24, 3)
    assert summary["distinct_ratio_window"] <= usage_log.REPETITION_TRIP_RATIO


def test_high_novelty_stays_above_the_trip_line() -> None:
    """The healthy shape: all-distinct signatures must never read as a loop."""
    summary = _summary([("search_files", {"q": f"p{i}"}) for i in range(48)])

    assert summary["distinct_ratio_window"] == 1.0
    assert summary["distinct_ratio_window"] > usage_log.REPETITION_TRIP_RATIO


def test_adjacent_repeat_the_46a2_shape_trips() -> None:
    """One identical call repeated adjacently (the 94a3/46a2 close trace)."""
    summary = _summary([("search_files", {"q": "same"})] * 40)

    assert summary["distinct_ratio_window"] == round(1 / 24, 3)
    assert summary["distinct_ratio_window"] <= usage_log.REPETITION_TRIP_RATIO


def test_short_all_identical_sequence_reports_none_not_a_trip() -> None:
    """2 identical calls over a whole-sequence denominator would read 0.5 and
    trip at call 2 — the false positive the None rule exists to prevent."""
    summary = _summary([("read_file", {"p": "a"})] * 2)

    assert summary["distinct_ratio_window"] is None


def test_empty_sequence_reports_none() -> None:
    summary = usage_log.run_shape([], request_limit=240, tool_calls_limit=480)

    assert summary["distinct_ratio_window"] is None


def test_exactly_window_size_is_the_first_measurable_point() -> None:
    below = _summary([("t", {"i": i}) for i in range(23)])
    at = _summary([("t", {"i": i}) for i in range(24)])

    assert below["distinct_ratio_window"] is None
    assert at["distinct_ratio_window"] == 1.0


def test_ratio_is_over_the_trailing_window_only() -> None:
    """30 novel calls then 24 identical: the trailing window sees 1 distinct —
    early healthy work must not dilute a late loop."""
    calls = [("t", {"i": i}) for i in range(30)] + [("t", {"same": 1})] * 24
    summary = _summary(calls)

    assert summary["distinct_ratio_window"] == round(1 / 24, 3)


def test_hand_computed_mixed_tail() -> None:
    """Trailing 24 = 12 distinct signatures, each twice -> 12/24 = 0.5, which
    sits exactly ON the trip line (a trip, per the <= contract)."""
    calls = [("t", {"novel": i}) for i in range(10)] + [("t", {"i": i % 12}) for i in range(24)]
    summary = _summary(calls)

    assert summary["distinct_ratio_window"] == 0.5


def test_constants_are_exported_beside_the_producer() -> None:
    assert usage_log.REPETITION_WINDOW == 24
    assert usage_log.REPETITION_TRIP_RATIO == 0.50


def test_format_repetition_renders_the_new_field() -> None:
    summary = _summary([("read_file", {"p": i % 4}) for i in range(48)])
    rendered = usage_log.format_repetition(summary)

    assert "distinct_ratio_window=0.167" in rendered
