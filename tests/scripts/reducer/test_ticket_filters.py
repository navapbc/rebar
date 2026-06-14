"""Unit tests for the shared ticket-list filter predicates (apply_ticket_filters).

These cover the filter SEMANTICS in isolation, on plain compiled-ticket dicts —
AND across dimensions, comma-OR within a dimension, priority exact-int match
(unset never matches), tag / without-tag ANY semantics, has+without
intersect-then-exclude, and the default error/fsck exclusion. Because
ticket-list.sh and ticket-lib-api.sh:ticket_list now share this one function,
a semantics regression is caught here for BOTH implementations and BOTH output
formats at the unit level.

Run: python3 -m pytest tests/scripts/test_ticket_filters.py -x
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from rebar.reducer import apply_ticket_filters  # noqa: E402


def _t(tid: str, **kw) -> dict:
    base = {
        "ticket_id": tid,
        "ticket_type": "task",
        "status": "open",
        "parent_id": "",
        "priority": None,
        "tags": [],
    }
    base.update(kw)
    return base


def _ids(rows) -> list:
    return sorted(r["ticket_id"] for r in rows)


@pytest.fixture()
def corpus() -> list:
    return [
        _t("a", ticket_type="epic", priority=0, tags=["rev"]),
        _t("b", ticket_type="epic", priority=0, tags=["rev", "brainstorm:complete"]),
        _t("c", ticket_type="epic", priority=1),
        _t("d", ticket_type="epic", priority=2, tags=["foo"]),
        _t("e", ticket_type="task", priority=0),
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_priority_exact_match(corpus) -> None:
    assert _ids(apply_ticket_filters(corpus, priority_filter="0")) == ["a", "b", "e"]


@pytest.mark.unit
@pytest.mark.scripts
def test_priority_comma_is_or(corpus) -> None:
    assert _ids(apply_ticket_filters(corpus, priority_filter="0,1")) == [
        "a",
        "b",
        "c",
        "e",
    ]


@pytest.mark.unit
@pytest.mark.scripts
def test_priority_unset_never_matches() -> None:
    rows = [_t("x", priority=None)]
    assert apply_ticket_filters(rows, priority_filter="0,1,2,3,4") == []


@pytest.mark.unit
@pytest.mark.scripts
def test_without_tag_excludes_any_listed(corpus) -> None:
    assert _ids(
        apply_ticket_filters(corpus, without_tag_filter="foo,brainstorm:complete")
    ) == ["a", "c", "e"]


@pytest.mark.unit
@pytest.mark.scripts
def test_has_tag_comma_is_or(corpus) -> None:
    assert _ids(apply_ticket_filters(corpus, tag_filter="rev,foo")) == ["a", "b", "d"]


@pytest.mark.unit
@pytest.mark.scripts
def test_has_and_without_intersect_then_exclude(corpus) -> None:
    assert _ids(
        apply_ticket_filters(
            corpus, tag_filter="rev", without_tag_filter="brainstorm:complete"
        )
    ) == ["a"]


@pytest.mark.unit
@pytest.mark.scripts
def test_and_across_dimensions_exemplar(corpus) -> None:
    """open P0 epics without brainstorm:complete -> a (the motivating exemplar)."""
    out = apply_ticket_filters(
        corpus,
        type_filter="epic",
        status_filter="open",
        priority_filter="0",
        without_tag_filter="brainstorm:complete",
    )
    assert _ids(out) == ["a"]


@pytest.mark.unit
@pytest.mark.scripts
def test_error_fsck_excluded_unless_requested() -> None:
    rows = [_t("ok"), _t("bad", status="error"), _t("f", status="fsck_needed")]
    assert _ids(apply_ticket_filters(rows)) == ["ok"]
    # Explicitly requesting the error status surfaces it (d145-e1a9).
    assert _ids(apply_ticket_filters(rows, status_filter="error")) == ["bad"]


@pytest.mark.unit
@pytest.mark.scripts
def test_no_filters_returns_all_as_new_list(corpus) -> None:
    out = apply_ticket_filters(corpus)
    assert out is not corpus  # does not mutate / alias the input
    assert _ids(out) == ["a", "b", "c", "d", "e"]
