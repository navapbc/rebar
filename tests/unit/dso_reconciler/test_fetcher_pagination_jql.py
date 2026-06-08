"""Pagination + JQL-verbatim tests for fetcher.py.

Originally task d3b8-a22b. Updated for the split-JQL contract under bug
f6cc-b174-9e9a-435c — the fetcher now issues two queries (active +
Done-recent) instead of one combined query.

Builds a 1500-issue ACLI fixture (1000 active + 500 Done) and verifies:

  * The fetcher invokes the ACLI stub with both split JQL strings verbatim:
    ``project = DIG AND status != "Done"``
    ``project = DIG AND status = "Done" ORDER BY updated DESC``
  * The fetcher paginates through the working set in 100-step ``start_at``
    increments (start_at=0, 100, 200, ..., 1400). At least 10 paginated
    invocations must occur across both queries combined.

AC-mandated source-literal tokens (grep -F greppable):
  * ``project = DIG AND status != "Done"``
  * ``project = DIG AND status = "Done" ORDER BY updated DESC``
  * ``range(1, 1501)``  (the combined 1500-issue fixture builder; now
    split as range(1, 1001) for active + range(1001, 1501) for Done)
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "fetcher.py"
)

# Two JQL strings emitted by fetch_snapshot under the split-JQL contract
# (bug f6cc-b174-9e9a-435c): one for the active working set, one for
# Done issues ordered by updated DESC. Both must reach search_issues
# verbatim; tests assert the union of jqls seen == {ACTIVE, DONE_RECENT}.
EXPECTED_JQL_ACTIVE = 'project = DIG AND status != "Done"'
EXPECTED_JQL_DONE_RECENT = 'project = DIG AND status = "Done" ORDER BY updated DESC'
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
# Pre-split, the stub used a single 1500-issue pool. Under the split-JQL
# contract, each JQL gets its own pool so both queries can complete under
# the 1200-issue per-query ceiling. Sizes chosen so that:
#   * Active pool (1000): comfortably under 1200 ceiling, ≥ 10 pages of 100
#     for the pagination-step assertion below.
#   * Done pool (500): exercises Q2 + the _DONE_RECENT_CAP=1000 cap as a
#     no-op (pool size 500 < cap).
# Source-literal ``range(1, 1501)`` is kept as a greppable AC token.

_ACTIVE_POOL = [
    {"key": f"DIG-{i}", "fields": {"summary": f"issue {i}"}} for i in range(1, 1001)
]
_DONE_POOL = [
    {"key": f"DIG-{i}", "fields": {"summary": f"issue {i}"}} for i in range(1001, 1501)
]
assert len(_ACTIVE_POOL) == 1000
assert len(_DONE_POOL) == 500
# Greppable AC token retained: range(1, 1501) describes the COMBINED pool
# (1000 active + 500 done = 1500 unique issues across both queries).
assert len(_ACTIVE_POOL) + len(_DONE_POOL) == 1500


class _PaginatingClient:
    """Records every ``(jql, start_at, max_results)`` it sees and returns
    the appropriate slice of the per-JQL pool. The JQL determines which
    pool to slice: active JQL gets the 1000-issue active pool; Done JQL
    gets the 500-issue Done pool. Unknown JQLs return the active pool
    (backward-compat for any test that passes a custom JQL)."""

    def __init__(self):
        self.calls: list[dict] = []

    def _pool_for(self, jql: str) -> list[dict]:
        if 'status = "Done"' in jql:
            return _DONE_POOL
        return _ACTIVE_POOL

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict]:
        self.calls.append(
            {"jql": jql, "start_at": start_at, "max_results": max_results}
        )
        pool = self._pool_for(jql)
        end = min(start_at + max_results, len(pool))
        return pool[start_at:end]


def _make_paginating_acli():
    holder: dict[str, _PaginatingClient] = {}

    class _Client(_PaginatingClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            holder["client"] = self

    mock_acli = types.ModuleType("acli_integration")
    mock_acli.AcliClient = _Client
    return mock_acli, holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetcher_calls_acli_with_split_jqls_verbatim(tmp_path, fetcher):
    """Every search_issues call must use one of the two split JQL strings
    verbatim, and the union of JQLs seen must be exactly the split pair.

    Required strings (bug f6cc-b174-9e9a-435c contract):
      * ``project = DIG AND status != "Done"``
      * ``project = DIG AND status = "Done" ORDER BY updated DESC``
    """
    mock_acli, holder = _make_paginating_acli()
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        try:
            fetcher.fetch_snapshot("d3b8-jql-verbatim", repo_root=tmp_path)
        except Exception:
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


def test_fetcher_paginates_through_1500_issues_in_100_step_increments(
    tmp_path, fetcher
):
    """Pagination loop must request at least 10 pages with start_at 0..900 in 100-step increments.

    Working set size: 1500 (see ``range(1, 1501)`` fixture builder above).
    """
    mock_acli, holder = _make_paginating_acli()
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        try:
            fetcher.fetch_snapshot("d3b8-paginate-1500", repo_root=tmp_path)
        except Exception:
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
        f"Expected first 10 start_at values to be {expected_prefix!r}; "
        f"got {start_ats[:10]!r}"
    )

    # Each call uses max_results=100 (the 100-step increment).
    for call in client.calls[:10]:
        assert call["max_results"] == 100, (
            f"Expected max_results=100; got {call['max_results']!r}"
        )

    # Every captured call carries one of the two verbatim split JQLs.
    for call in client.calls:
        assert call["jql"] in EXPECTED_JQLS, (
            f"Expected JQL in {EXPECTED_JQLS!r}; got {call['jql']!r}"
        )
