"""HELD-OUT: the inbound snapshot pager (bug deac, epic e369).

THE DEFECT. ``_iter_pages`` terminated on a SHORT page and advanced by the REQUESTED
page size:

    if len(page) < page_size:
        return
    start_at += page_size

Jira Data Center silently truncates ``maxResults`` above ``jira.search.views.default.max``
— a common hardening, and the same server behaviour that produced the identical defect in
``get_parent_map`` (fixed in change 1105). A truncated FIRST page therefore reads as "that
is all there is".

WHY THIS ONE IS THE WORST OF THE THREE SIBLINGS. ``collect()`` feeds ``_build_snapshot``,
so a truncated read here is not a missing attribute on a present issue — it is an issue the
pass never sees at all. Every unseen bound issue becomes a candidate for the absence/deletion
path, and a run in that state reports success: nothing raises, and "converged" is true of
the fraction it looked at. Measured before the fix: **20 of 250 issues, 92% lost.**

WHY THE CLOUD ASSERTIONS BELOW ARE NOT PADDING. This is CORE code on the path the live Cloud
bridge also uses, so "DC is fixed" is not sufficient evidence. The ``cap``, ACLI-ceiling and
same-token-twice behaviours are pinned here so a regression in any of them fails loudly
rather than surfacing as a mis-sized Cloud snapshot.
"""

from __future__ import annotations

import pytest

from rebar_reconciler import fetcher
from rebar_reconciler.fetcher import SilentTruncationError


class _PageCappingClient:
    """Serves ``total`` issues but caps EVERY page at ``server_cap``, whatever is asked.

    This is a lowered ``jira.search.views.default.max``, the documented DC hardening —
    not a contrived shape.
    """

    def __init__(self, total: int = 250, server_cap: int = 20) -> None:
        self.issues = [{"key": f"DC-{i}", "fields": {}} for i in range(total)]
        self.server_cap = server_cap
        self.calls = 0

    def search_issues(self, jql, start_at=0, max_results=50):
        self.calls += 1
        return self.issues[start_at : start_at + min(max_results, self.server_cap)]


def test_a_server_capped_page_does_not_end_the_scan() -> None:
    """THE BUG. A short page is NOT proof of exhaustion — it is what a hardened DC
    instance returns for every page."""
    client = _PageCappingClient(total=250, server_cap=20)
    got = fetcher.collect(client, "project = DC", page_size=100)
    assert len(got) == 250, (
        f"recovered {len(got)} of 250 issues — the pager treated a server-truncated page "
        f"as the final one and silently dropped {250 - len(got)} issues from the INBOUND "
        f"SNAPSHOT. Every dropped bound issue is a candidate for the deletion path, and "
        f"the pass reports success either way"
    )
    assert [i["key"] for i in got] == [f"DC-{i}" for i in range(250)], (
        "the recovered set is not the full ordered set — the pager skipped or repeated"
    )


def test_an_exactly_full_final_page_terminates_on_the_empty_page() -> None:
    """EDGE. When the total is an exact multiple of the page size the old code stopped
    without an extra request; the new code needs one empty page to learn it is done. That
    is a correct, bounded cost — assert it terminates rather than loops."""
    client = _PageCappingClient(total=40, server_cap=20)
    got = fetcher.collect(client, "project = DC", page_size=20)
    assert len(got) == 40
    assert client.calls == 3, (
        f"expected 2 full pages + 1 empty terminator, got {client.calls} calls"
    )


def test_a_server_that_ignores_start_at_on_a_full_page_raises_rather_than_looping() -> None:
    """TEETH for the fix itself. Driving the loop off the RETURNED page means a server
    that ignores ``startAt`` re-serves the same page forever. When that page is FULL the
    server is claiming there is more, so the loop never terminates — and quietly
    returning what we have would be the silent partial this function exists to refuse.
    The old same-token-twice guard covers the Cloud CURSOR stall, not an offset one."""

    class _StuckClient:
        def __init__(self) -> None:
            self.calls = 0

        def search_issues(self, jql, start_at=0, max_results=50):
            self.calls += 1
            if self.calls > 100:  # bounded so a hang fails the test, not the suite
                raise AssertionError("pager looped: no offset-stall guard fired")
            return [{"key": f"DC-{i}", "fields": {}} for i in range(10)]  # full, forever

    client = _StuckClient()
    with pytest.raises(SilentTruncationError) as excinfo:
        fetcher.collect(client, "project = DC", page_size=10)
    assert excinfo.value.reason == "offset-stall"
    assert client.calls <= 100, "the guard did not fire before the bounded ceiling"


def test_a_non_offset_aware_client_returning_a_short_page_stops_cleanly() -> None:
    """The OTHER half of the stall rule, and the reason it is not simply "raise on any
    repeat". A client that returns fewer items than requested and then repeats itself is
    not offset-aware and has nothing further to give — we already hold everything it can
    produce, so stopping is correct and raising would be a false alarm. Many existing
    test doubles have exactly this shape.

    Crucially this does NOT weaken the DC fix: a hardened DC HONOURS ``startAt``, so its
    second capped page holds different issues (asserted above) and the scan continues."""

    class _NonOffsetAwareClient:
        def __init__(self) -> None:
            self.calls = 0
            self.issues = [{"key": f"DC-{i}", "fields": {}} for i in range(3)]

        def search_issues(self, jql, **kwargs):  # ignores start_at entirely
            self.calls += 1
            return list(self.issues)

    client = _NonOffsetAwareClient()
    got = fetcher.collect(client, "project = DC", page_size=100)
    assert [i["key"] for i in got] == ["DC-0", "DC-1", "DC-2"], (
        f"a non-offset-aware client's items were duplicated or lost: {got!r}"
    )
    assert client.calls == 2, (
        f"expected one page + one confirming re-query, got {client.calls} calls"
    )


# ---------------------------------------------------------------------------
# The Cloud contract, pinned. This is shared code — "DC works" is not evidence.
# ---------------------------------------------------------------------------


def test_the_client_side_cap_still_clips() -> None:
    """``cap`` is an INTENTIONAL client-side truncation and must keep working: it stops
    cleanly and does NOT raise, unlike the silent-truncation paths."""
    client = _PageCappingClient(total=250, server_cap=20)
    got = fetcher.collect(client, "project = DC", page_size=100, cap=45)
    assert len(got) == 45, f"cap=45 yielded {len(got)}"


def test_the_acli_ceiling_still_raises() -> None:
    """The ACLI working-set ceiling must still refuse to yield a silently-truncated set.
    A pager fix that drove past the ceiling would trade one silent truncation for another."""
    ceiling = fetcher._ACLI_CEILING
    client = _PageCappingClient(total=ceiling + 500, server_cap=1000)
    with pytest.raises(SilentTruncationError) as excinfo:
        fetcher.collect(client, "project = DC", page_size=1000)
    assert excinfo.value.reason == "ceiling"


def test_the_same_token_twice_cursor_stall_still_raises() -> None:
    """Cloud's cursor-stall detection is independent of the offset guard and must survive."""

    class _StallingCursorClient:
        next_page_token = "same-token"

        def __init__(self) -> None:
            self.issues = [{"key": f"C-{i}", "fields": {}} for i in range(100)]

        def search_issues(self, jql, start_at=0, max_results=50):
            return self.issues[start_at : start_at + max_results]

    with pytest.raises(SilentTruncationError) as excinfo:
        fetcher.collect(_StallingCursorClient(), "project = C", page_size=10)
    assert excinfo.value.reason == "same-token-twice"


def test_an_empty_result_set_yields_nothing_without_raising() -> None:
    """An empty project is a normal state, not a truncation."""

    class _EmptyClient:
        def search_issues(self, jql, start_at=0, max_results=50):
            return []

    assert fetcher.collect(_EmptyClient(), "project = DC", page_size=100) == []
