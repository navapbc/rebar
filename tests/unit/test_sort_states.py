"""Unit tests for ``reads.sort_states`` (P1.1 result ordering).

Pure (hand-built state dicts) so the None-last discipline and tie-break are
pinned directly — normal tickets never carry a None sort key (priority/created/
updated/id/status are always set), so the None-last guard is only reachable here
and for error/fsck dicts.
"""

from __future__ import annotations

from rebar._engine_support.reads import sort_key_valid, sort_states


def _ids(rows):
    return [r["ticket_id"] for r in rows]


def test_empty_sort_is_identity():
    rows = [{"ticket_id": "b"}, {"ticket_id": "a"}]
    assert sort_states(rows, "") is rows  # unchanged, default order


def test_unknown_sort_is_identity():
    rows = [{"ticket_id": "b"}, {"ticket_id": "a"}]
    assert sort_states(rows, "bogus") is rows


def test_ascending_and_descending():
    rows = [
        {"ticket_id": "b", "priority": 2},
        {"ticket_id": "c", "priority": 1},
        {"ticket_id": "a", "priority": 3},
    ]
    assert _ids(sort_states(rows, "priority")) == ["c", "b", "a"]
    assert _ids(sort_states(rows, "-priority")) == ["a", "b", "c"]


def test_none_sorts_last_in_both_directions():
    rows = [
        {"ticket_id": "b", "priority": 2},
        {"ticket_id": "a", "priority": None},  # unset → must sort LAST
        {"ticket_id": "c", "priority": 1},
    ]
    assert _ids(sort_states(rows, "priority")) == ["c", "b", "a"]
    assert _ids(sort_states(rows, "-priority")) == ["b", "c", "a"]


def test_tiebreak_is_ticket_id_ascending_regardless_of_direction():
    rows = [
        {"ticket_id": "y", "priority": 1},
        {"ticket_id": "x", "priority": 1},
        {"ticket_id": "z", "priority": 1},
    ]
    assert _ids(sort_states(rows, "priority")) == ["x", "y", "z"]
    assert _ids(sort_states(rows, "-priority")) == ["x", "y", "z"]


def test_mixed_none_does_not_raise_typeerror():
    # The `(is_none, value)` partitioning must avoid `None < int` (TypeError).
    rows = [{"ticket_id": "a", "priority": None}, {"ticket_id": "b", "priority": 0}]
    assert _ids(sort_states(rows, "priority")) == ["b", "a"]


def test_sort_key_valid():
    assert sort_key_valid("")  # no-op
    assert sort_key_valid("priority")
    assert sort_key_valid("-updated")
    assert sort_key_valid("id")
    assert not sort_key_valid("bogus")
    assert not sort_key_valid("-nonsense")
