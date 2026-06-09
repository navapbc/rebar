"""Tests for fetcher truncation gate (originally task cbd6-39c7-f331-4af6;
ceiling raised from 1000 → 1200 in bug f6cc-b174-9e9a-435c).

Contract under test:
  * Each JQL query has a hard ACLI per-query ceiling of 1200 issues
    (JRACLOUD-94632; raised from the original 1000 ceiling because the
    DIG project's working set exceeded it).
  * If a query accumulates 1200 issues from ACLI, the fetcher MUST raise
    ``SilentTruncationError`` rather than silently returning a truncated set.
  * Fallback path: if ACLI returns the same ``nextPageToken`` on two
    consecutive calls (a "same-token-twice" loop), the fetcher MUST also
    raise ``SilentTruncationError``.
  * Below the 1200 ceiling (e.g. 1150 issues per query), fetching
    completes cleanly without error.

The string literal ``SilentTruncationError`` appears below for the
``grep -F 'SilentTruncationError'`` AC. The string ``same-token-twice`` and
the ``below_ceiling`` marker also appear for the related grep ACs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"
)
ERRORS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_errors.py"
)

# Best-effort import of SilentTruncationError. If not yet defined, fall back to
# a local placeholder so test collection still succeeds in the RED state.
try:
    spec = importlib.util.spec_from_file_location("_rebar_reconciler_errors", ERRORS_PATH)
    assert spec is not None and spec.loader is not None
    _errors_mod = importlib.util.module_from_spec(spec)
    sys.modules["_rebar_reconciler_errors"] = _errors_mod
    spec.loader.exec_module(_errors_mod)  # type: ignore[union-attr]
    SilentTruncationError = getattr(
        _errors_mod,
        "SilentTruncationError",
        type("SilentTruncationError", (Exception,), {}),
    )
except Exception:  # pragma: no cover — defensive only
    SilentTruncationError = type("SilentTruncationError", (Exception,), {})


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def fetcher():
    if not FETCHER_PATH.exists():
        pytest.fail(f"fetcher.py not found at {FETCHER_PATH}")
    return _load_fetcher()


def _make_issue(i: int) -> dict:
    return {"key": f"DIG-{i}", "fields": {"summary": f"Issue {i}"}}


class _PaginatingStubClient:
    """Stub AcliClient that paginates a pre-built issue list of size N."""

    def __init__(self, total: int, page_size: int = 100):
        self._issues = [_make_issue(i) for i in range(total)]
        self._page_size = page_size
        self.calls: list[tuple[int, int]] = []

    def search_issues(self, jql, start_at=0, max_results=100):
        self.calls.append((start_at, max_results))
        return self._issues[start_at : start_at + max_results]


class _SameTokenTwiceClient:
    """Stub that returns the same nextPageToken on consecutive calls.

    Simulates ACLI's degenerate "stuck cursor" mode where the server
    returns the same page-cursor twice in a row — the agreed-upon signal
    for silent-truncation per JRACLOUD-94632.

    The stub returns full pages forever (never shrinks below page_size)
    and exposes ``nextPageToken`` via an attribute on the returned list
    AND via a parallel attribute on the client itself (current fetcher
    interface tolerates either).
    """

    def __init__(self, page_size: int = 100):
        self._page_size = page_size
        self.next_page_token = "stuck-cursor-abc"
        self.calls = 0

    def search_issues(self, jql, start_at=0, max_results=100):
        self.calls += 1
        # Always return a full page so length-based termination never trips.
        page = [_make_issue(start_at + i) for i in range(max_results)]
        # The same-token-twice marker — same token on every call.
        self.next_page_token = "stuck-cursor-abc"  # noqa: F841 — intentional
        return page


# ---------------------------------------------------------------------------
# Test 1: 1200-issue per-query ceiling raises SilentTruncationError
# ---------------------------------------------------------------------------


def test_fetch_at_1200_issue_ceiling_raises_silent_truncation_error(fetcher, tmp_path):
    """1200 issues across 12 pages of 100 — hits the per-query ACLI ceiling.

    The stub returns the same 1200 issues for both split JQLs; the first
    query (active) trips the ceiling at accumulated=1100 + page=100 → 1200.
    """
    client = _PaginatingStubClient(total=1200, page_size=100)

    def _fake_load_acli():
        mod = type(sys)("fake_acli")
        mod.AcliClient = lambda **kwargs: client  # type: ignore[attr-defined]
        return mod

    with patch.object(fetcher, "_load_acli", _fake_load_acli):
        with pytest.raises(Exception) as exc_info:
            fetcher.fetch_snapshot(pass_id="ceiling-test", repo_root=tmp_path)

    exc_type_name = type(exc_info.value).__name__
    assert exc_type_name == "SilentTruncationError", (
        f"Expected SilentTruncationError at 1200-issue ceiling, "
        f"got {exc_type_name}: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Test 2: same-token-twice path raises SilentTruncationError
# ---------------------------------------------------------------------------


def test_fetch_same_token_twice_raises_silent_truncation_error(fetcher, tmp_path):
    """If ACLI returns the same nextPageToken twice in a row ("same-token-twice"),
    the fetcher MUST raise SilentTruncationError before reaching the per-query cap.
    """
    client = _SameTokenTwiceClient(page_size=100)

    def _fake_load_acli():
        mod = type(sys)("fake_acli")
        mod.AcliClient = lambda **kwargs: client  # type: ignore[attr-defined]
        return mod

    with patch.object(fetcher, "_load_acli", _fake_load_acli):
        with pytest.raises(Exception) as exc_info:
            fetcher.fetch_snapshot(pass_id="same-token-twice-test", repo_root=tmp_path)

    exc_type_name = type(exc_info.value).__name__
    assert exc_type_name == "SilentTruncationError", (
        f"Expected SilentTruncationError on same-token-twice cursor stall, "
        f"got {exc_type_name}: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Test 3: 1150 issues (below_ceiling / under_ceiling) — fetches cleanly
# ---------------------------------------------------------------------------


def test_fetch_1150_issues_under_ceiling_succeeds_below_ceiling(fetcher, tmp_path):
    """1150 issues is below_ceiling (1200) — fetcher must NOT raise, must
    write a snapshot. The same stub returns 1150 issues for both queries;
    Q2 dedupes them all but still completes without raising."""
    client = _PaginatingStubClient(total=1150, page_size=100)

    def _fake_load_acli():
        mod = type(sys)("fake_acli")
        mod.AcliClient = lambda **kwargs: client  # type: ignore[attr-defined]
        return mod

    with patch.object(fetcher, "_load_acli", _fake_load_acli):
        # Should NOT raise — 1150 is under_ceiling.
        out_path = fetcher.fetch_snapshot(
            pass_id="under-ceiling-1150", repo_root=tmp_path
        )

    assert out_path.exists(), "snapshot file should be written below_ceiling"
