"""P1.1: structured-query predicates + ``--sort``, across library / CLI, with a
back-compat guarantee that a predicate-free search is byte-identical to before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rebar


def _env(repo: Path) -> dict:
    e = dict(os.environ)
    e["REBAR_ROOT"] = str(repo)
    return e


def _cli_search(repo: Path, *args: str) -> list:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "search", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def _ids(results) -> list:
    return [t["ticket_id"] for t in results]


def test_field_predicate_status_and_priority(rebar_repo: Path) -> None:
    a = rebar.create_ticket("bug", "alpha widget", priority=0)
    b = rebar.create_ticket("bug", "beta widget", priority=3)
    rebar.create_ticket("task", "gamma widget", priority=0)  # wrong type

    # status:open priority:<2 type:bug  → only the P0 bug.
    hits = set(_ids(rebar.search("widget status:open priority:<2 type:bug")))
    assert hits == {a}
    assert b not in hits


def test_comma_or_within_field(rebar_repo: Path) -> None:
    a = rebar.create_ticket("task", "one thing")
    b = rebar.create_ticket("task", "two thing")
    rebar.transition(b, "open", "in_progress")
    c = rebar.create_ticket("task", "three thing")
    rebar.transition(c, "open", "in_progress")
    rebar.transition(c, "in_progress", "closed")

    hits = set(_ids(rebar.search("thing status:open,in_progress")))
    assert hits == {a, b}  # closed c excluded


def test_negation(rebar_repo: Path) -> None:
    a = rebar.create_ticket("task", "keeper alpha")
    b = rebar.create_ticket("task", "keeper beta")
    rebar.tag(b, "wip")

    hits = set(_ids(rebar.search("keeper -tag:wip")))
    assert hits == {a}


def test_tag_and_assignee_predicates(rebar_repo: Path) -> None:
    a = rebar.create_ticket("task", "assigned work", assignee="alice")
    rebar.tag(a, "backend")
    rebar.create_ticket("task", "other work", assignee="bob")

    assert set(_ids(rebar.search("work assignee:alice"))) == {a}
    assert set(_ids(rebar.search("work tag:backend"))) == {a}


def test_unknown_field_is_substring(rebar_repo: Path) -> None:
    # An unknown field must degrade to a literal substring, not crash or filter.
    a = rebar.create_ticket("task", "has foo:bar literal in title")
    rebar.create_ticket("task", "unrelated")
    assert set(_ids(rebar.search("foo:bar"))) == {a}


def test_sort_priority_ascending_and_descending(rebar_repo: Path) -> None:
    p0 = rebar.create_ticket("task", "sortable p0", priority=0)
    p2 = rebar.create_ticket("task", "sortable p2", priority=2)
    p4 = rebar.create_ticket("task", "sortable p4", priority=4)

    asc = _ids(rebar.search("sortable", sort="priority"))
    assert asc == [p0, p2, p4]
    desc = _ids(rebar.search("sortable", sort="-priority"))
    assert desc == [p4, p2, p0]


def test_sort_id_tiebreak_is_ascending_both_directions(rebar_repo: Path) -> None:
    # Same priority for all → the ticket_id tie-break orders them, ascending
    # regardless of the primary sort direction (stable two-stage sort).
    a = rebar.create_ticket("task", "tie work", priority=2)
    b = rebar.create_ticket("task", "tie work two", priority=2)
    asc_ids = sorted([a, b])
    assert _ids(rebar.search("tie", sort="priority")) == asc_ids
    # Equal primary key ⇒ direction does not flip the tie-break.
    assert _ids(rebar.search("tie", sort="-priority")) == asc_ids


def test_cli_library_parity_predicate_and_sort(rebar_repo: Path) -> None:
    rebar.create_ticket("bug", "parity widget", priority=0)
    rebar.create_ticket("bug", "parity gadget", priority=2)

    lib = rebar.search("parity type:bug", sort="-priority")
    cli = _cli_search(rebar_repo, "parity type:bug", "--sort=-priority")
    assert _ids(lib) == _ids(cli)


def test_plain_search_backcompat_unchanged(rebar_repo: Path) -> None:
    # A predicate-free query must return exactly what the historical
    # whitespace-AND substring search returned: title/description/comment/tag,
    # AND across terms, case-insensitive.
    a = rebar.create_ticket("task", "Zephyr login flow")
    b = rebar.create_ticket("task", "other", description="zephyr handling")
    rebar.create_ticket("task", "unrelated")

    assert set(_ids(rebar.search("zephyr"))) == {a, b}
    assert set(_ids(rebar.search("zephyr login"))) == {a}  # AND
    assert set(_ids(rebar.search("ZEPHYR"))) == {a, b}  # case-insensitive


def test_sort_by_updated_orders_by_derived_timestamp(rebar_repo: Path) -> None:
    # The one sort dimension that depends on derived state. Touch the tickets in
    # a known order so updated_at strictly increases, then assert -updated puts
    # the most-recently-touched first. (Also guards against the cache serving a
    # stale updated_at=None.)
    a = rebar.create_ticket("task", "touch alpha")
    b = rebar.create_ticket("task", "touch beta")
    c = rebar.create_ticket("task", "touch gamma")
    rebar.comment(c, "touch c first")
    rebar.comment(b, "touch b second")
    rebar.comment(a, "touch a last")  # a is now the most recently updated

    assert _ids(rebar.search("touch", sort="-updated")) == [a, b, c]
    assert _ids(rebar.search("touch", sort="updated")) == [c, b, a]


def test_list_sort(rebar_repo: Path) -> None:
    p0 = rebar.create_ticket("task", "list p0", priority=0)
    p3 = rebar.create_ticket("task", "list p3", priority=3)
    ids = [t["ticket_id"] for t in rebar.list_tickets(sort="priority")]
    # p0 precedes p3 in ascending priority order.
    assert ids.index(p0) < ids.index(p3)


def test_ready_sort(rebar_repo: Path) -> None:
    p0 = rebar.create_ticket("task", "ready p0", priority=0)
    p4 = rebar.create_ticket("task", "ready p4", priority=4)
    ids = [t["ticket_id"] for t in rebar.ready(sort="-priority")]
    assert ids.index(p4) < ids.index(p0)  # descending → p4 first


def test_priority_range_predicate_e2e(rebar_repo: Path) -> None:
    p0 = rebar.create_ticket("bug", "range p0", priority=0)
    p2 = rebar.create_ticket("bug", "range p2", priority=2)
    p4 = rebar.create_ticket("bug", "range p4", priority=4)
    hits = set(_ids(rebar.search("range priority:1..3")))
    assert hits == {p2}
    assert p0 not in hits and p4 not in hits
    # Open-ended: *..2 keeps 0 and 2, drops 4.
    assert set(_ids(rebar.search("range priority:*..2"))) == {p0, p2}


def test_parent_alias_resolution_e2e(rebar_repo: Path) -> None:
    epic = rebar.create_ticket("epic", "parent epic", return_alias=True)
    child = rebar.create_ticket("task", "child of epic", parent=epic["id"])
    rebar.create_ticket("task", "unrelated child")
    # parent: accepts the human alias and resolves it to the canonical id.
    assert set(_ids(rebar.search("child parent:" + epic["alias"]))) == {child}
    # ...and the canonical id works too.
    assert set(_ids(rebar.search("child parent:" + epic["id"]))) == {child}


def test_invalid_sort_key_is_usage_error(rebar_repo: Path) -> None:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "search", "x", "--sort=bogus"],
        cwd=str(rebar_repo),
        capture_output=True,
        text=True,
        env=_env(rebar_repo),
    )
    assert cp.returncode == 2
    assert "--sort" in cp.stderr
