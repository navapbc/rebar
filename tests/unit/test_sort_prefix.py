"""Unit tests for the integer event-prefix comparator (P2.1, epic snappy-weed-ruin).

``reducer._sort.event_sort_key`` compares the ``${timestamp}`` filename prefix as
an **integer**, so legacy 19-digit ns names and (hypothetical) wider HLC names sort
into one global order. String comparison only agrees while every name is the same
width — this is the width-comparator gap the integer key closes.
"""

from __future__ import annotations

from rebar.reducer._sort import event_sort_key, prefix_ts


def _name(prefix: str, etype: str = "EDIT") -> str:
    return f"{prefix}-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-{etype}.json"


def test_prefix_ts_parses_integer():
    assert prefix_ts(_name("1781386734104724847")) == 1781386734104724847
    assert prefix_ts("/abs/path/" + _name("42")) == 42  # basename only


def test_prefix_ts_malformed_falls_back_to_minus_one():
    # No leading digits → deterministic sentinel, sorts before any real event.
    assert prefix_ts(".cache.json") == -1
    assert prefix_ts("notanumber-uuid-EDIT.json") == -1


def test_integer_width_comparator_beats_string_order():
    # 19 nines ≈ 10^19 - 1; the 20-digit name is 10^19 — numerically LARGER, so it
    # must sort AFTER. String comparison would put "1000…" (starts '1') before
    # "9999…" (starts '9') — the exact width bug integer comparison fixes.
    legacy = _name("9999999999999999999")  # 19 digits
    wider = _name("10000000000000000000")  # 20 digits, 10^19
    assert int("10000000000000000000") > int("9999999999999999999")
    assert sorted([wider, legacy], key=event_sort_key) == [legacy, wider]
    # String order would have disagreed:
    assert sorted([wider, legacy]) == [wider, legacy]


def test_equal_width_int_order_matches_string_order():
    # The common case (all 19-digit) must be unchanged: int order == string order.
    names = [_name(str(p)) for p in (1781386734104724847, 1781386734104724999, 1781386734104724000)]
    assert sorted(names, key=event_sort_key) == sorted(names)


def test_link_sorts_before_unlink_at_same_prefix():
    p = "1781386734104724847"
    link = _name(p, "LINK")
    unlink = _name(p, "UNLINK")
    assert sorted([unlink, link], key=event_sort_key) == [link, unlink]
