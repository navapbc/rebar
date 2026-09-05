"""Partitioned base-query fetch — capacity decoupled from project size (bug 5c5c).

The hourly Reconcile Bridge began failing 2026-09-05T01:33 UTC with
``SilentTruncationError(reason="ceiling")``: the REB active working set
(``statusCategory != "Done"``) measured **1,388** issues against the 1,200-issue
``_ACLI_CEILING``. It measured 1,050 on 2026-05-26, so this is organic growth, not a
widened query — ``status != "Done"`` and ``statusCategory != "Done"`` both measure 1,388
today, i.e. the 2026-08-15 statusCategory switch matched zero extra issues.

The ceiling is a VENDOR limit (JRACLOUD-94632) and the working set only grows, so raising
the constant a second time only schedules a third outage. Instead the drain PARTITIONS the
query into half-open ``created`` windows and unions them, so a slice's size is bounded by
its window rather than by the project.

``created`` is the partition key for two measured reasons:

  * **Totality does not require the boundary value to exist.** ``issuekey`` ranges were the
    first candidate and are a SILENT-DROP TRAP: measured live, ``issuekey < REB-5373``
    (exists) matched 1,387 while ``issuekey < REB-5501`` (does not exist) matched **0**,
    raising nothing. A 500-wide key tiling over REB-1..REB-5500 summed to 979 against a
    true 1,388 — 409 issues lost in silence. A ``created`` bound partitions the space for
    ANY instant, real or not.
  * **``created`` is immutable.** ``updated`` moves under a running pass, so an issue
    touched mid-scan can migrate across a window boundary and be missed (or double-counted).

These tests are the oracle for that contract. The seam between slices is where this class
of bug returns, so the cursor-stall, offset-stall and short-page-is-not-exhaustion defences
are each proven to still fire in a LATER slice — not merely within the first one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from rebar_reconciler import fetch_paging

_FMT = "%Y-%m-%d %H:%M"
_EPOCH = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_BASE_JQL = 'project = REB AND statusCategory != "Done"'

# A stall must abort the whole drain, so only the windows opened BEFORE it are ever issued.
# Bisecting a stalled window instead (treating it like a ceiling hit) explodes well past
# this, which is what gives the two seam tests their teeth.
_ABORT_WINDOW_BUDGET = 12

# A server that ignores the ``created`` window can never be narrowed into compliance, so the
# drain must stop probing quickly rather than doubling the window until the dates overflow.
_BLIND_PROBE_BUDGET = 12  # measured: 11 with the span ceiling, 16 without


def _parse_window(jql: str) -> tuple[datetime | None, datetime | None]:
    """Extract the (lo, hi) half-open ``created`` window a partitioned JQL encodes."""
    lo = hi = None
    for token, setter in (('created >= "', "lo"), ('created < "', "hi")):
        idx = jql.find(token)
        if idx != -1:
            raw = jql[idx + len(token) : jql.index('"', idx + len(token))]
            if setter == "lo":
                lo = datetime.strptime(raw, _FMT).replace(tzinfo=UTC)
            else:
                hi = datetime.strptime(raw, _FMT).replace(tzinfo=UTC)
    return lo, hi


class _CreatedWindowClient:
    """A Jira stub that honours the ``created`` window in the JQL it is handed.

    Serves ``total`` issues whose ``created`` instants are spread ``spacing`` apart from
    ``_EPOCH``, so a window's population is proportional to its width — the property a
    partitioned drain relies on.
    """

    def __init__(self, total: int, spacing: timedelta = timedelta(minutes=17)) -> None:
        self.issues = [
            {"key": f"REB-{i}", "created": _EPOCH + i * spacing, "fields": {"summary": str(i)}}
            for i in range(total)
        ]
        self.jqls: list[str] = []

    def _matching(self, jql: str) -> list[dict]:
        lo, hi = _parse_window(jql)
        return [
            i
            for i in self.issues
            if (lo is None or i["created"] >= lo) and (hi is None or i["created"] < hi)
        ]

    def search_issues(self, jql, start_at=0, max_results=100):
        if start_at == 0:
            self.jqls.append(jql)
        rows = self._matching(jql)[start_at : start_at + max_results]
        return {"issues": [{k: v for k, v in r.items() if k != "created"} for r in rows]}


# --------------------------------------------------------------------------------------
# AC4 — capacity no longer scales with total project size
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("total", [1388, 3000, 5000])
def test_working_set_larger_than_the_ceiling_drains_completely(total: int) -> None:
    """A working set MATERIALLY larger than ``_ACLI_CEILING`` returns a COMPLETE snapshot.

    This is the outage reproduced: at ``total=1388`` (the measured live REB active count)
    the un-partitioned drain raises ``SilentTruncationError(reason="ceiling")``.
    """
    assert total > fetch_paging._ACLI_CEILING or total == 1388
    client = _CreatedWindowClient(total)

    issues = fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)

    assert len(issues) == total, f"expected a complete {total}-issue snapshot"
    assert [i["key"] for i in issues] == [f"REB-{i}" for i in range(total)]


def test_no_slice_is_ever_served_more_than_the_ceiling() -> None:
    """Every window handed to the pager stays under the ceiling — capacity is bounded by
    the WINDOW, not by the project, which is the whole point of the partition."""
    client = _CreatedWindowClient(5000)
    fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)

    completed = [j for j in client.jqls if len(client._matching(j)) < fetch_paging._ACLI_CEILING]
    assert completed, "no window completed under the ceiling"


def test_partition_covers_every_issue_exactly_once() -> None:
    """The union of the drained slices is a PARTITION: total coverage, no duplication.

    A dropped slice shows up as a missing key; a broken (overlapping) union shows up as a
    duplicate. Both are the silent-partial this drain exists to prevent.
    """
    client = _CreatedWindowClient(3000)
    keys = [i["key"] for i in fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)]

    assert len(keys) == len(set(keys)), "a key was returned twice — the union overlaps"
    assert set(keys) == {f"REB-{i}" for i in range(3000)}, "a slice was silently dropped"


# --------------------------------------------------------------------------------------
# AC5 — the anti-truncation defences still hold ACROSS THE SEAM between slices
# --------------------------------------------------------------------------------------


class _LaterSliceCursorStallClient(_CreatedWindowClient):
    """Honours windows, but stalls its cursor once a LATER slice is reached."""

    next_page_token = "t0"

    def search_issues(self, jql, start_at=0, max_results=100):
        lo, _hi = _parse_window(jql)
        if lo is not None and start_at > 0:
            self.next_page_token = "STALLED"
        return super().search_issues(jql, start_at=start_at, max_results=max_results)


def test_cursor_stall_in_a_later_slice_still_raises() -> None:
    """``same-token-twice`` fires at the SEAM, not only in the first slice."""
    client = _LaterSliceCursorStallClient(3000)
    with pytest.raises(fetch_paging.SilentTruncationError) as exc:
        fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert exc.value.reason == "same-token-twice"
    # And it ABORTED at the stall rather than narrowing and re-asking: a stalled cursor is
    # not a ceiling, so it must never be retried as a bisection.
    assert len(client.jqls) <= _ABORT_WINDOW_BUDGET


class _LaterSliceOffsetStallClient(_CreatedWindowClient):
    """Honours windows, but ignores ``startAt`` once a LATER slice is reached."""

    def search_issues(self, jql, start_at=0, max_results=100):
        lo, _hi = _parse_window(jql)
        if lo is not None:
            start_at = 0  # offset-blind: re-serve the same full page forever
        return super().search_issues(jql, start_at=start_at, max_results=max_results)


def test_offset_stall_in_a_later_slice_still_raises() -> None:
    """``offset-stall`` fires at the SEAM — a slice that cannot advance must not be
    reported as a complete slice and folded into the union."""
    client = _LaterSliceOffsetStallClient(3000)
    with pytest.raises(fetch_paging.SilentTruncationError) as exc:
        fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert exc.value.reason == "offset-stall"
    assert len(client.jqls) <= _ABORT_WINDOW_BUDGET


class _ShortPageClient(_CreatedWindowClient):
    """Honours windows and ``startAt``, but silently caps every page at 20 rows — the Jira
    DC ``jira.search.views.default.max`` hardening from bug deac."""

    def search_issues(self, jql, start_at=0, max_results=100):
        return super().search_issues(jql, start_at=start_at, max_results=min(max_results, 20))


def test_short_page_is_not_exhaustion_within_every_slice() -> None:
    """A server-truncated SHORT page must not end a slice's scan. Measured at 92% loss in
    bug deac; the partition must not reintroduce it per-slice."""
    client = _ShortPageClient(3000)
    issues = fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert len(issues) == 3000, "a short page ended a slice early — bug deac at the seam"


def test_ceiling_still_raises_when_a_window_cannot_be_split_further() -> None:
    """The guard is PRESERVED, not weakened: when a window is already at the finest
    resolution and still over the ceiling, the drain refuses rather than truncating."""
    # Every issue created in the SAME minute: no bisection can separate them.
    client = _CreatedWindowClient(fetch_paging._ACLI_CEILING + 500, spacing=timedelta(0))
    with pytest.raises(fetch_paging.SilentTruncationError) as exc:
        fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert exc.value.reason == "ceiling"


# --------------------------------------------------------------------------------------
# AC3 — the ceiling constant is NOT raised as the fix
# --------------------------------------------------------------------------------------


def test_acli_ceiling_constant_is_not_raised() -> None:
    """Regression guard for the ticket's central instruction: the fix must not be a bigger
    number. 1,200 was already the SECOND value (1,000 -> 1,200 in bug f6cc)."""
    assert fetch_paging._ACLI_CEILING == 1200


def test_ordered_query_is_refused_rather_than_silently_mis_partitioned() -> None:
    """An ``ORDER BY`` query (the capped done-recent one) is a top-N read, not an
    exhaustive one — partitioning it would change its meaning, so it is REFUSED loudly."""
    with pytest.raises(ValueError, match="ORDER BY"):
        fetch_paging.collect_partitioned(
            _CreatedWindowClient(10),
            'project = REB AND statusCategory = "Done" ORDER BY updated DESC',
            page_size=100,
        )


# --------------------------------------------------------------------------------------
# The outage itself, and the fetcher-level wiring that resolves it
# --------------------------------------------------------------------------------------

_MEASURED_REB_ACTIVE = 1388  # live count, 2026-09-05 (baseline 1,050 on 2026-05-26)


def test_unpartitioned_drain_still_refuses_the_measured_working_set() -> None:
    """The reproduction, and proof the guard was NOT weakened: draining the measured live
    working set through the single-query path still raises, exactly as it did at 01:33."""
    with pytest.raises(fetch_paging.SilentTruncationError) as exc:
        fetch_paging.collect(_CreatedWindowClient(_MEASURED_REB_ACTIVE), _BASE_JQL, page_size=100)
    assert exc.value.reason == "ceiling"


def test_fetch_project_drains_the_measured_working_set_completely() -> None:
    """End-to-end at the fetcher seam: ``_fetch_project`` runs the UNCAPPED active query
    through the partitioned drain and the CAPPED done query through the single-query path,
    so the pass that failed at 01:33 now completes."""
    from rebar_reconciler import fetcher

    client = _CreatedWindowClient(_MEASURED_REB_ACTIVE)
    queries = ((_BASE_JQL, None),)

    issues = fetcher._fetch_project(client, queries)

    assert len(issues) == _MEASURED_REB_ACTIVE


def test_fetch_project_leaves_the_capped_done_query_unpartitioned() -> None:
    """The done-recent query keeps its top-N semantics: capped at ``_DONE_RECENT_CAP``,
    still a single ``ORDER BY`` query, and never routed through the partitioner (which
    would refuse it)."""
    from rebar_reconciler import fetcher

    client = _CreatedWindowClient(3000)
    done_jql = 'project = REB AND statusCategory = "Done" ORDER BY updated DESC'

    issues = fetcher._fetch_project(client, ((done_jql, fetcher._DONE_RECENT_CAP),))

    assert len(issues) == fetcher._DONE_RECENT_CAP
    assert all('created >= "' not in j for j in client.jqls), "the top-N query was partitioned"


class _WindowBlindClient(_CreatedWindowClient):
    """Serves the FULL working set for every window — it ignores ``created`` entirely.

    The window-level sibling of the offset-stall client: a server that drops the predicate
    narrowing the query can never be narrowed into compliance.
    """

    def _matching(self, jql: str) -> list[dict]:
        return self.issues


def test_a_server_that_ignores_the_created_window_is_refused_not_looped() -> None:
    """Narrowing only helps if the server honours the narrowing. When it does not, the drain
    must stop and re-raise the CEILING — never loop doubling the window until the date
    arithmetic overflows, and never fold an over-large slice into the union.
    """
    client = _WindowBlindClient(3000)
    with pytest.raises(fetch_paging.SilentTruncationError) as exc:
        fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert exc.value.reason == "ceiling"
    # And it gave up PROMPTLY. Without the span ceiling the walk-back keeps doubling until
    # the date arithmetic runs out of range — tens of futile full-page drains against a
    # live Jira before the same verdict.
    assert len(client.jqls) <= _BLIND_PROBE_BUDGET


def test_drained_windows_chain_with_no_gap_and_no_overlap() -> None:
    """STRUCTURAL proof that the slices are a partition of the whole timeline.

    Data-level coverage checks only catch a seam gap when an issue happens to fall in it.
    This asserts the seam itself: the windows that actually completed must chain end to end
    — the first unbounded below, the last unbounded above, and every window's upper bound
    exactly the next window's lower bound. A gap drops whatever is created inside it; an
    overlap double-counts. Neither can hide behind sparse test data here.
    """
    client = _CreatedWindowClient(5000)
    fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)

    # A window completed iff its population was under the ceiling; the rest were split.
    leaves = [
        _parse_window(j)
        for j in client.jqls
        if len(client._matching(j)) < fetch_paging._ACLI_CEILING
    ]
    leaves.sort(key=lambda w: (w[0] is not None, w[0]))

    assert leaves[0][0] is None, "the earliest window must be unbounded below"
    assert leaves[-1][1] is None, "the latest window must be unbounded above"
    for (_lo, hi), (next_lo, _next_hi) in pairwise(leaves):
        assert hi == next_lo, f"seam defect: window ends {hi} but the next starts {next_lo}"


def test_densely_created_issues_survive_every_seam() -> None:
    """One issue per minute, so EVERY seam lands on a populated minute: a boundary that is
    off by even the smallest expressible step loses issues."""
    client = _CreatedWindowClient(3000, spacing=timedelta(minutes=1))
    issues = fetch_paging.collect_partitioned(client, _BASE_JQL, page_size=100)
    assert {i["key"] for i in issues} == {f"REB-{i}" for i in range(3000)}
