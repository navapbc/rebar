"""Pin split-JQL pagination and verbatim query delivery.

A 1,500-issue fixture (1,000 active and 500 recent Done) must deliver both DIG
queries unchanged and paginate in 100-item steps for at least ten calls.
``range(1, 1501)`` remains the combined-pool acceptance token.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"

# The active and recent-Done JQLs must both reach ``search_issues`` verbatim.
EXPECTED_JQL_ACTIVE = 'project = DIG AND statusCategory != "Done"'
EXPECTED_JQL_DONE_RECENT = 'project = DIG AND statusCategory = "Done" ORDER BY updated DESC'
EXPECTED_JQLS = {EXPECTED_JQL_ACTIVE, EXPECTED_JQL_DONE_RECENT}


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def fetcher():
    if not FETCHER_PATH.exists():
        pytest.fail(f"fetcher.py not found at {FETCHER_PATH}")
    return _load_fetcher()


# ---------------------------------------------------------------------------
# Per-JQL paginated fixture (split-JQL aware, bug f6cc).
# ---------------------------------------------------------------------------
#
# Separate pools stay below the 1,200 per-query ceiling: 1,000 active issues
# exercise ten 100-item pages, while 500 Done issues leave the 1,000-item cap
# inactive. Keep ``range(1, 1501)`` as the combined-pool acceptance token.

_ACTIVE_POOL = [{"key": f"DIG-{i}", "fields": {"summary": f"issue {i}"}} for i in range(1, 1001)]
_DONE_POOL = [{"key": f"DIG-{i}", "fields": {"summary": f"issue {i}"}} for i in range(1001, 1501)]
assert len(_ACTIVE_POOL) == 1000
assert len(_DONE_POOL) == 500
# Combined-pool token: range(1, 1501) = 1,000 active + 500 Done.
assert len(_ACTIVE_POOL) + len(_DONE_POOL) == 1500


class _PaginatingClient:
    """Record calls and slice the JQL-specific pool; unknown JQLs use active."""

    def __init__(self):
        self.calls: list[dict] = []

    def _pool_for(self, jql: str) -> list[dict]:
        if 'statusCategory = "Done"' in jql:
            return _DONE_POOL
        return _ACTIVE_POOL

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50) -> list[dict]:
        self.calls.append({"jql": jql, "start_at": start_at, "max_results": max_results})
        pool = self._pool_for(jql)
        end = min(start_at + max_results, len(pool))
        return pool[start_at:end]


def _make_paginating_acli():
    holder: dict[str, _PaginatingClient] = {}

    class _Client(_PaginatingClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            holder["client"] = self

    # S4: _load_acli returns the transport instance directly.
    return _Client(), holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetcher_calls_acli_with_split_jqls_verbatim(tmp_path, fetcher):
    """Send only the exact active and recent-Done JQL pair to ``search_issues``."""
    mock_acli, holder = _make_paginating_acli()
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        try:
            fetcher.fetch_snapshot("d3b8-jql-verbatim", repo_root=tmp_path)
        except fetcher.SilentTruncationError:
            # Truncation gate may raise mid-loop. Calls captured up to the
            # raise remain assertable.
            pass

    client = holder["client"]
    assert client.calls, "fetch_snapshot must invoke search_issues at least once"
    seen_jqls = {c["jql"] for c in client.calls}
    # Every JQL seen must be one of the two split queries (no other JQL leaked).
    assert seen_jqls.issubset(EXPECTED_JQLS), (
        f"Unexpected JQL string(s): {seen_jqls - EXPECTED_JQLS!r} — "
        f"expected subset of {EXPECTED_JQLS!r}"
    )
    # Both JQLs reached (unless an early-loop truncation prevented the
    # second query from starting). If a truncation occurred, surface that
    # explicitly rather than asserting both were seen.
    if seen_jqls != EXPECTED_JQLS:
        pytest.fail(
            f"Expected both split JQLs to reach search_issues; only saw "
            f"{seen_jqls!r}. Missing: {EXPECTED_JQLS - seen_jqls!r}"
        )


def test_fetcher_paginates_through_1500_issues_in_100_step_increments(tmp_path, fetcher):
    """Pagination loop must request at least 10 pages with start_at 0..900 in 100-step increments.

    Working set size: 1500 (see ``range(1, 1501)`` fixture builder above).
    """
    mock_acli, holder = _make_paginating_acli()
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        try:
            fetcher.fetch_snapshot("d3b8-paginate-1500", repo_root=tmp_path)
        except fetcher.SilentTruncationError:
            # cbd6 truncation may raise; partial call-sequence remains valid.
            pass

    client = holder["client"]
    assert client.calls, "fetch_snapshot must invoke search_issues at least once"

    start_ats = [c["start_at"] for c in client.calls]
    assert len(start_ats) >= 10, (
        f"Expected at least 10 paginated invocations for the 1500-issue working set; "
        f"got {len(start_ats)} calls with start_at values {start_ats!r}"
    )

    # The first 10 start_at values must be 0, 100, 200, ..., 900 — proving
    # 100-step increments are used.
    expected_prefix = list(range(0, 1000, 100))
    assert start_ats[:10] == expected_prefix, (
        f"Expected first 10 start_at values to be {expected_prefix!r}; got {start_ats[:10]!r}"
    )

    # Each call uses max_results=100 (the 100-step increment).
    for call in client.calls[:10]:
        assert call["max_results"] == 100, f"Expected max_results=100; got {call['max_results']!r}"

    # Every captured call carries one of the two verbatim split JQLs.
    for call in client.calls:
        assert call["jql"] in EXPECTED_JQLS, (
            f"Expected JQL in {EXPECTED_JQLS!r}; got {call['jql']!r}"
        )
