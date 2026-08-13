"""Oracle for the pagination-cluster split (ticket a33c).

The per-query pagination machinery — ``_ACLI_CEILING``, ``SilentTruncationError``,
``_extract_issues``, ``_iter_pages`` and ``collect`` — moved from
``rebar_reconciler.fetcher`` into a new ``rebar_reconciler.fetch_paging``. The move is
behaviour-preserving, so this oracle proves (a) every moved name still resolves through the
``fetcher`` paths its consumers use and is the SAME object as in ``fetch_paging``, (b) the
split is one-way (``fetch_paging`` does not import ``fetcher``), (c) draining through either
surface yields identical results and both raise ``SilentTruncationError`` on the ceiling, and
(d) both files sit under the size target.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rebar_reconciler import fetch_paging, fetcher

_MOVED = (
    "_ACLI_CEILING",
    "SilentTruncationError",
    "_extract_issues",
    "_iter_pages",
    "collect",
)


def test_moved_names_resolve_from_fetcher_and_are_object_identical() -> None:
    """Every moved name still resolves from ``fetcher`` (the re-export its callers use) and
    is the SAME object as in ``fetch_paging`` — the form tests and callers import."""
    for name in _MOVED:
        assert hasattr(fetcher, name), f"{name} no longer resolves from fetcher"
        assert getattr(fetcher, name) is getattr(fetch_paging, name)


def test_split_is_one_way_no_import_of_fetcher() -> None:
    """``fetch_paging`` must not import ``fetcher`` (else the split is not one-way)."""
    tree = ast.parse(Path(fetch_paging.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod not in {"fetcher", "rebar_reconciler.fetcher"}, (
                f"one-way violation: fetch_paging imports from {mod}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in {"fetcher", "rebar_reconciler.fetcher"}


class _PageClient:
    """search_issues stub: serves ``total`` issues in ``page_size``-sized slices."""

    def __init__(self, total: int) -> None:
        self._issues = [{"key": f"DC-{i}"} for i in range(total)]

    def search_issues(self, jql, start_at=0, max_results=100):
        return {"issues": self._issues[start_at : start_at + max_results]}


def test_both_surfaces_drain_identically() -> None:
    """``collect`` reached via ``fetcher`` and via ``fetch_paging`` returns the same issues —
    the two entry points route through one implementation."""
    via_fetcher = fetcher.collect(_PageClient(250), "project = DC", page_size=100)
    via_paging = fetch_paging.collect(_PageClient(250), "project = DC", page_size=100)
    assert via_fetcher == via_paging
    assert [i["key"] for i in via_fetcher] == [f"DC-{i}" for i in range(250)]


def test_ceiling_still_raises_silent_truncation() -> None:
    """The per-query ACLI ceiling still raises ``SilentTruncationError`` through the
    re-exported surface — the guard survived the move."""
    with pytest.raises(fetcher.SilentTruncationError):
        fetcher.collect(_PageClient(fetcher._ACLI_CEILING + 50), "project = DC", page_size=100)


def test_both_modules_under_the_size_target() -> None:
    """AC: each file at or below 650 lines (asserted explicitly, not via the 800 gate)."""
    for mod in (fetcher, fetch_paging):
        n = len(Path(mod.__file__).read_text().splitlines())
        assert n <= 650, f"{Path(mod.__file__).name} is {n} lines (> 650)"
