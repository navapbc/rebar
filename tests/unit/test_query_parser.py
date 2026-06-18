"""Unit tests for the P1.1 structured-query parser and shared comparison
primitive: ``reducer/_query.parse_query`` and ``reducer/_filters.match_predicate``.

Pure (no tracker / no I/O) — they pin the grammar and the one comparison
vocabulary shared by the query path and ``apply_ticket_filters``.
"""

from __future__ import annotations

from rebar.reducer._filters import match_predicate
from rebar.reducer._query import parse_query


# ───────────────────────────── parse_query ───────────────────────────────────
def test_plain_query_is_text_terms_only() -> None:
    # Back-compat: a predicate-free query is just lowercased substring terms,
    # AND-combined — identical to the historical whitespace search.
    preds, terms = parse_query("Login Fix")
    assert preds == []
    assert terms == [("login", False), ("fix", False)]


def test_known_field_becomes_predicate() -> None:
    preds, terms = parse_query("status:open")
    assert preds == [("status", "eq", "open", False)]
    assert terms == []


def test_comma_is_or_within_field() -> None:
    preds, _ = parse_query("status:open,in_progress")
    assert preds == [("status", "in", {"open", "in_progress"}, False)]


def test_priority_range_operators() -> None:
    assert parse_query("priority:<2")[0] == [("priority", "lt", "2", False)]
    assert parse_query("priority:>=1")[0] == [("priority", "ge", "1", False)]
    assert parse_query("priority:1..3")[0] == [("priority", "range", ("1", "3"), False)]
    assert parse_query("priority:*..2")[0] == [("priority", "range", (None, "2"), False)]
    assert parse_query("priority:2..*")[0] == [("priority", "range", ("2", None), False)]


def test_negation_on_predicate_and_text() -> None:
    preds, terms = parse_query("-status:closed -login not:tag:wip")
    assert ("status", "eq", "closed", True) in preds
    assert ("tag", "eq", "wip", True) in preds
    assert ("login", True) in terms


def test_unknown_field_degrades_to_substring() -> None:
    # Unknown field:value must NOT raise and must NOT become a predicate.
    preds, terms = parse_query("foo:bar")
    assert preds == []
    assert terms == [("foo:bar", False)]


def test_bare_or_is_not_a_keyword() -> None:
    # There is no bare OR operator in v1 — a literal "OR" is free text.
    preds, terms = parse_query("status:open OR status:closed")
    assert ("or", False) in terms
    # Both predicates survive as an AND (never matches), proving OR is not boolean.
    assert ("status", "eq", "open", False) in preds
    assert ("status", "eq", "closed", False) in preds


# ───────────────────────────── match_predicate ───────────────────────────────
_ST = {
    "status": "open",
    "ticket_type": "bug",
    "priority": 1,
    "assignee": "alice",
    "tags": ["backend", "p1"],
    "parent_id": "epic-1",
}


def test_match_string_fields_exact() -> None:
    assert match_predicate(_ST, "status", "eq", "open")
    assert not match_predicate(_ST, "status", "eq", "closed")
    assert match_predicate(_ST, "type", "eq", "bug")  # field alias -> ticket_type
    assert match_predicate(_ST, "assignee", "eq", "alice")
    assert match_predicate(_ST, "parent", "eq", "epic-1")  # alias -> parent_id


def test_match_in_set() -> None:
    assert match_predicate(_ST, "status", "in", {"open", "closed"})
    assert not match_predicate(_ST, "status", "in", {"closed", "deleted"})


def test_match_tag_membership() -> None:
    assert match_predicate(_ST, "tag", "eq", "backend")
    assert not match_predicate(_ST, "tag", "eq", "frontend")
    assert match_predicate(_ST, "tag", "in", {"frontend", "p1"})


def test_match_priority_operators() -> None:
    assert match_predicate(_ST, "priority", "lt", "2")
    assert match_predicate(_ST, "priority", "le", "1")
    assert not match_predicate(_ST, "priority", "gt", "1")
    assert match_predicate(_ST, "priority", "ge", "1")
    assert match_predicate(_ST, "priority", "range", ("0", "2"))
    assert not match_predicate(_ST, "priority", "range", ("2", None))
    assert match_predicate(_ST, "priority", "in", {"1", "3"})


def test_unset_priority_never_matches() -> None:
    st = {"priority": None}
    assert not match_predicate(st, "priority", "lt", "5")
    assert not match_predicate(st, "priority", "eq", "0")


def test_uncomparable_value_is_false_not_raise() -> None:
    # A non-integer priority value must yield False, never a TypeError.
    assert not match_predicate(_ST, "priority", "lt", "notanint")
    assert not match_predicate({}, "status", "eq", "open")
