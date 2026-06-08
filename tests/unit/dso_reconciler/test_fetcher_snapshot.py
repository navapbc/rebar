"""Tests for dso_reconciler/fetcher.py — new generator + filtered-JQL contract.

Asserts the post-10e3 contract:
  * fetcher exposes ``_iter_pages(client, jql, page_size)`` — a generator that
    yields one page (list[dict]) per call to ``client.search_issues``.
  * fetcher exposes ``collect(client, jql, page_size=...)`` — a thin wrapper
    that drains ``_iter_pages`` into a single list.
  * ``fetch_snapshot`` issues the **filtered** JQL string verbatim:
    ``project = DIG AND (resolution = Unresolved OR updated >= -1h)``.
  * Pagination stub follows the shape
    ``callable(jql, start_at, max_results) -> {issues, startAt, maxResults, total}``
    (mirrors task-5e13 shared conftest fixture; defined locally here so this
    file collects cleanly even on a session HEAD that doesn't yet have 5e13).

RED state for fetcher.py is acceptable per task ACs — current fetcher emits
the OLD shape (no ``_iter_pages``, full-project JQL).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "fetcher.py"
)

# Split-JQL contract (bug f6cc-b174-9e9a-435c). fetch_snapshot now issues
# TWO queries (active first, then Done-recent). Both must reach
# search_issues verbatim.
EXPECTED_JQL_ACTIVE = 'project = DIG AND status != "Done"'
EXPECTED_JQL_DONE_RECENT = 'project = DIG AND status = "Done" ORDER BY updated DESC'
EXPECTED_JQLS = {EXPECTED_JQL_ACTIVE, EXPECTED_JQL_DONE_RECENT}
# Neutral JQL for direct _iter_pages / collect calls — those helpers
# accept any JQL string; the JQL contract is on fetch_snapshot, not them.
ANY_JQL = EXPECTED_JQL_ACTIVE


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def fetcher():
    """Load the fetcher module, failing all tests if absent."""
    if not FETCHER_PATH.exists():
        pytest.fail(
            f"fetcher.py not found at {FETCHER_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_fetcher()


# ---------------------------------------------------------------------------
# Local paginating stub
# ---------------------------------------------------------------------------
#
# Mirrors the shared ``paginating_acli_stub`` fixture introduced by task 5e13.
# When 5e13 lands and the conftest exposes that fixture, tests below can be
# migrated to consume it directly; until then a locally-defined stub keeps
# this file self-contained and collectable.


class _PaginatingClient:
    """Stub Jira client. callable(jql, start_at, max_results)."""

    def __init__(self, total: int = 250, page_size: int = 100):
        self._total = total
        self._page_size = page_size
        self.calls: list[dict] = []

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50) -> dict:
        self.calls.append(
            {"jql": jql, "start_at": start_at, "max_results": max_results}
        )
        end = min(start_at + max_results, self._total)
        issues = [
            {"key": f"DIG-{i}", "fields": {"summary": f"issue {i}"}}
            for i in range(start_at, end)
        ]
        return {
            "issues": issues,
            "startAt": start_at,
            "maxResults": max_results,
            "total": self._total,
        }


def _make_paginating_acli(total: int = 250, page_size: int = 100):
    client_holder: dict[str, _PaginatingClient] = {}

    class _Client(_PaginatingClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__(total=total, page_size=page_size)
            client_holder["client"] = self

    mock_acli = types.ModuleType("acli_integration")
    mock_acli.AcliClient = _Client
    return mock_acli, client_holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_iter_pages_is_a_generator(fetcher):
    """fetcher exposes a ``_iter_pages`` generator yielding one page per call."""
    assert hasattr(fetcher, "_iter_pages"), (
        "fetcher must expose `_iter_pages(client, jql, page_size)`"
    )
    client = _PaginatingClient(total=250, page_size=100)
    gen = fetcher._iter_pages(client, ANY_JQL, page_size=100)
    # generator semantics: yields pages, not an aggregated list
    import types as _t

    assert isinstance(gen, _t.GeneratorType), (
        "_iter_pages must return a generator, not a materialized list"
    )
    pages = list(gen)
    assert len(pages) == 3, "Expected 3 pages for total=250, page_size=100"
    assert all(isinstance(p, list) for p in pages), "Each page must be a list"
    assert len(pages[0]) == 100
    assert len(pages[1]) == 100
    assert len(pages[2]) == 50


def test_collect_wrapper_drains_iter_pages(fetcher):
    """fetcher exposes a ``collect`` wrapper that flattens all pages."""
    assert hasattr(fetcher, "collect"), (
        "fetcher must expose `collect(client, jql, page_size=...)`"
    )
    client = _PaginatingClient(total=250, page_size=100)
    issues = fetcher.collect(client, ANY_JQL, page_size=100)
    assert isinstance(issues, list)
    assert len(issues) == 250
    assert issues[0]["key"] == "DIG-0"
    assert issues[-1]["key"] == "DIG-249"


def test_collect_passes_jql_to_client_unchanged(fetcher):
    """The JQL string passed to ``collect`` reaches every search_issues call
    verbatim (collect is jql-string-agnostic — fetch_snapshot owns the
    split-JQL contract; see test_fetch_snapshot_uses_split_jqls below).
    """
    client = _PaginatingClient(total=120, page_size=100)
    fetcher.collect(client, ANY_JQL, page_size=100)
    assert client.calls, "collect must invoke search_issues at least once"
    for call in client.calls:
        assert call["jql"] == ANY_JQL, f"Expected JQL {ANY_JQL!r}, got {call['jql']!r}"


def test_fetch_snapshot_uses_split_jqls(tmp_path, fetcher):
    """fetch_snapshot issues both split JQLs verbatim:
    * ``project = DIG AND status != "Done"``
    * ``project = DIG AND status = "Done" ORDER BY updated DESC``
    """
    mock_acli, holder = _make_paginating_acli(total=10, page_size=100)
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        fetcher.fetch_snapshot("2026-05-24-pass-jql", repo_root=tmp_path)
    client = holder["client"]
    assert client.calls, "fetch_snapshot must call search_issues at least once"
    seen_jqls = {c["jql"] for c in client.calls}
    assert seen_jqls == EXPECTED_JQLS, (
        f"fetch_snapshot must pass both split JQLs verbatim; "
        f"expected {EXPECTED_JQLS!r}, saw {seen_jqls!r}"
    )


def test_fetch_snapshot_written_to_correct_path(tmp_path, fetcher):
    """fetch_snapshot writes the file to bridge_state/snapshots/<pass_id>.json."""
    pass_id = "2026-05-24-pass-01"
    mock_acli, _ = _make_paginating_acli(total=5, page_size=100)

    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        result_path = fetcher.fetch_snapshot(pass_id, repo_root=tmp_path)

    expected_path = tmp_path / "bridge_state" / "snapshots" / f"{pass_id}.json"
    assert result_path == expected_path
    assert expected_path.exists()


def test_fetch_snapshot_is_valid_json(tmp_path, fetcher):
    """fetch_snapshot produces a file that parses as valid JSON keyed by issue key."""
    pass_id = "2026-05-24-pass-02"
    mock_acli, _ = _make_paginating_acli(total=2, page_size=100)

    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        result_path = fetcher.fetch_snapshot(pass_id, repo_root=tmp_path)

    parsed = json.loads(result_path.read_text())
    assert isinstance(parsed, dict)
    assert "DIG-0" in parsed
    assert "DIG-1" in parsed


def test_fetch_snapshot_is_deterministic(tmp_path, fetcher):
    """Two fetch_snapshot calls with identical stub data produce byte-identical files."""
    mock_acli, _ = _make_paginating_acli(total=5, page_size=100)

    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        path_a = fetcher.fetch_snapshot("2026-05-24-pass-03a", repo_root=tmp_path)

    mock_acli2, _ = _make_paginating_acli(total=5, page_size=100)
    with patch.object(fetcher, "_load_acli", return_value=mock_acli2):
        path_b = fetcher.fetch_snapshot("2026-05-24-pass-03b", repo_root=tmp_path)

    assert path_a.read_bytes() == path_b.read_bytes(), (
        "Two fetches with identical data must produce byte-identical snapshots"
    )


def test_fetch_snapshot_paginates_through_full_result_set(tmp_path, fetcher):
    """fetch_snapshot must page through `collect()` to capture all issues.

    Stubs 250 issues across 3 pages (100, 100, 50). Every issue must
    land in the snapshot.
    """
    mock_acli, holder = _make_paginating_acli(total=250, page_size=100)

    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        result_path = fetcher.fetch_snapshot(
            "2026-05-24-pass-pagination", repo_root=tmp_path
        )

    parsed = json.loads(result_path.read_text())
    assert len(parsed) == 250
    assert "DIG-0" in parsed
    assert "DIG-249" in parsed
    # Every search_issues call carried one of the two split JQLs.
    client = holder["client"]
    for call in client.calls:
        assert call["jql"] in EXPECTED_JQLS, (
            f"Expected JQL in {EXPECTED_JQLS!r}; got {call['jql']!r}"
        )


def test_fetch_snapshot_search_error_propagates(tmp_path, fetcher):
    """Errors raised by AcliClient.search_issues() propagate out of fetch_snapshot."""

    class _ErrorClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_issues(self, jql: str, **kwargs):
            raise RuntimeError("ACLI connection refused")

    mock_acli = types.ModuleType("acli_integration")
    mock_acli.AcliClient = _ErrorClient

    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        with pytest.raises(RuntimeError, match="ACLI connection refused"):
            fetcher.fetch_snapshot("2026-05-24-pass-05", repo_root=tmp_path)
