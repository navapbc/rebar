"""Behavioral tests for the split-JQL fetcher contract (bug f6cc-b174-9e9a-435c).

Pre-fix, ``fetcher.fetch_snapshot`` issued a single JQL
(``project = DIG AND (resolution = Unresolved OR updated >= -1h)``) and
hit the 1000-issue ACLI ceiling because the DIG project has > 1000
issues spanning To Do + In Progress + Done.

Post-fix, ``fetch_snapshot`` issues TWO queries in order:
  1. ``project = DIG AND status != "Done"`` — the active working set,
     no client-side cap (only the per-query ACLI ceiling bounds it).
  2. ``project = DIG AND status = "Done" ORDER BY updated DESC`` — capped
     to ``_DONE_RECENT_CAP`` (1000) most-recently-updated Done issues.

These tests assert observable behavior of that contract:
  * Both queries reach the ACLI client verbatim, in order.
  * The Done-recent query is capped at _DONE_RECENT_CAP regardless of the
    actual stub size, and the cap stops consumption cleanly (does NOT
    raise SilentTruncationError).
  * The active query is NOT capped (consumes everything the stub returns).
  * The per-query ACLI ceiling raised from 1000 → 1200 (regression guard).
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


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fetcher():
    return _load_fetcher()


def _make_issue(n: int) -> dict:
    return {"key": f"DIG-{n}", "fields": {"summary": f"issue {n}"}}


class _JqlRoutedClient:
    """Stub Jira client whose per-JQL response size is configurable.

    sizes maps each JQL to its total result count; pages are sliced
    [start_at : start_at+max_results] from a list of synthetic issues
    keyed DIG-0..DIG-(size-1) per JQL with a JQL-specific key prefix
    so cross-query duplicates can be distinguished in assertions.
    """

    def __init__(self, sizes: dict[str, int], page_size: int = 100):
        self._sizes = sizes
        self._page_size = page_size
        self.calls: list[dict] = []

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict]:
        self.calls.append(
            {"jql": jql, "start_at": start_at, "max_results": max_results}
        )
        size = self._sizes.get(jql, 0)
        if start_at >= size:
            return []
        end = min(start_at + max_results, size)
        # Key prefix encodes the JQL so cross-query dedup can be observed.
        prefix = "ACT" if "!=" in jql else "DONE"
        return [_make_issue(f"{prefix}-{i}") for i in range(start_at, end)]


def _make_acli_module_returning(client):
    mock_acli = types.ModuleType("acli_integration")
    mock_acli.AcliClient = lambda *a, **k: client  # type: ignore[attr-defined]
    return mock_acli


def test_both_split_jqls_issued_in_order_active_then_done(tmp_path, fetcher):
    """fetch_snapshot calls search_issues for the active JQL first, then
    the Done-recent JQL — observable from client.calls in order."""
    client = _JqlRoutedClient(
        sizes={
            fetcher.JQL_ACTIVE: 150,
            fetcher.JQL_DONE_RECENT: 80,
        }
    )
    with patch.object(
        fetcher, "_load_acli", return_value=_make_acli_module_returning(client)
    ):
        fetcher.fetch_snapshot("split-order-test", repo_root=tmp_path)

    # First call must use the active JQL; later calls switch to Done-recent.
    assert client.calls[0]["jql"] == fetcher.JQL_ACTIVE, (
        f"Expected first call to use active JQL; got {client.calls[0]['jql']!r}"
    )
    seen_jqls_in_order = [c["jql"] for c in client.calls]
    active_indices = [
        i for i, j in enumerate(seen_jqls_in_order) if j == fetcher.JQL_ACTIVE
    ]
    done_indices = [
        i for i, j in enumerate(seen_jqls_in_order) if j == fetcher.JQL_DONE_RECENT
    ]
    # Both queries reached — guard before calling max/min on indices.
    # (Without these guards the assertion below would crash with ValueError
    # instead of a diagnostic assertion failure if a regression caused Q1
    # to raise before Q2 began.)
    assert active_indices, (
        f"Active JQL never reached search_issues. JQL sequence: "
        f"{seen_jqls_in_order!r}"
    )
    assert done_indices, (
        f"Done-recent JQL never reached search_issues — Q1 likely raised "
        f"before Q2 began. JQL sequence: {seen_jqls_in_order!r}"
    )
    # All active calls precede all done calls (no interleaving).
    assert max(active_indices) < min(done_indices), (
        "Active and Done queries interleaved; expected all active calls before "
        f"any Done call. JQL sequence: {seen_jqls_in_order!r}"
    )


def test_done_query_capped_at_done_recent_cap(tmp_path, fetcher):
    """When the Done-recent stub holds MORE than _DONE_RECENT_CAP issues,
    fetch_snapshot consumes only _DONE_RECENT_CAP of them — and does NOT
    raise SilentTruncationError (the cap is intentional client-side
    truncation, not silent ACLI truncation)."""
    cap = fetcher._DONE_RECENT_CAP
    client = _JqlRoutedClient(
        sizes={
            fetcher.JQL_ACTIVE: 10,
            fetcher.JQL_DONE_RECENT: cap + 100,  # 100 more than the cap
        }
    )
    with patch.object(
        fetcher, "_load_acli", return_value=_make_acli_module_returning(client)
    ):
        out_path = fetcher.fetch_snapshot("done-cap-test", repo_root=tmp_path)

    # Count Done issues consumed = sum of max_results actually returned for Done JQL.
    done_consumed = 0
    for c in client.calls:
        if c["jql"] == fetcher.JQL_DONE_RECENT:
            size = client._sizes[c["jql"]]
            page_len = min(c["max_results"], max(0, size - c["start_at"]))
            done_consumed += page_len
    # The cap may clip the final page, so we count what reaches the snapshot.
    import json as _json

    snapshot = _json.loads(out_path.read_text())
    done_in_snapshot = [k for k in snapshot if k.startswith("DIG-DONE-")]
    assert len(done_in_snapshot) == cap, (
        f"Done query should be capped at {cap} issues; "
        f"snapshot has {len(done_in_snapshot)} Done items"
    )


def test_active_query_uncapped_consumes_everything_under_ceiling(tmp_path, fetcher):
    """The active query has no client-side cap; it consumes all issues the
    stub returns, bounded only by the per-query ACLI ceiling."""
    # 1100 active issues — under the 1200 ceiling.
    client = _JqlRoutedClient(
        sizes={
            fetcher.JQL_ACTIVE: 1100,
            fetcher.JQL_DONE_RECENT: 0,
        }
    )
    with patch.object(
        fetcher, "_load_acli", return_value=_make_acli_module_returning(client)
    ):
        out_path = fetcher.fetch_snapshot("active-uncapped-test", repo_root=tmp_path)

    import json as _json

    snapshot = _json.loads(out_path.read_text())
    active_in_snapshot = [k for k in snapshot if k.startswith("DIG-ACT-")]
    assert len(active_in_snapshot) == 1100, (
        f"Active query should consume all 1100 stub issues; "
        f"snapshot has {len(active_in_snapshot)}"
    )


# Behavioral coverage for the three constants (_ACLI_CEILING, _DONE_RECENT_CAP,
# JQLS) is provided by the tests above and by test_fetcher_truncation_gate.py:
#   * _ACLI_CEILING enforcement:
#       test_fetch_at_1200_issue_ceiling_raises_silent_truncation_error
#       (test_fetcher_truncation_gate.py)
#   * _DONE_RECENT_CAP enforcement:
#       test_done_query_capped_at_done_recent_cap (above)
#   * JQLS / query ordering:
#       test_both_split_jqls_issued_in_order_active_then_done (above)
# Per the behavioral testing standard, separate "constant-equals-N"
# regression-guard tests are change-detector tests and were intentionally
# omitted: they break on safe refactorings and add no observable-behavior
# assurance beyond what the behavioral tests already give.
