"""ACLI pagination helpers extracted from ``fetcher``.

Holds the per-query pagination machinery: the ACLI ceiling constant, the
``SilentTruncationError`` raised on silent truncation, and the
``_iter_pages``/``collect`` generators that drain a paged ``search_issues``
result while guarding against ceiling, cursor-stall and offset-stall
truncation. Split out of ``fetcher`` (bug a33c) to keep that module under the
size cap; ``fetcher`` re-exports these names for its existing callers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport


# Hard ACLI per-query ceiling. Raised from 1,000 to 1,200 in bug f6cc
# after empirical confirmation that the DIG working set has 1,050 active
# issues + 1,120 Done issues (probe 2026-05-26). 1,200 covers active
# with ~150-issue headroom and bounds the Done query under its 1,000-
# issue cap (see _DONE_RECENT_CAP). If either query exceeds this ceiling
# again, raise SilentTruncationError rather than silently truncating.
_ACLI_CEILING = 1200


class SilentTruncationError(Exception):
    """Raised when ACLI silently truncates the result set.

    Two trigger conditions:
      * Accumulated issue count reaches the 1000-issue ACLI ceiling.
      * ACLI returns the same ``next_page_token`` on two consecutive calls
        ("same-token-twice" cursor-stall mode).
    """

    def __init__(self, message: str = "", reason: str = "") -> None:
        super().__init__(message or reason or "silent truncation detected")
        self.reason = reason


def _extract_issues(result) -> list[dict]:
    """Normalize a search_issues result to a list of issue dicts.

    ACLI stubs and the real client return either a bare list or a dict shaped
    ``{"issues": [...], "startAt": ..., "total": ...}``. Accept both.
    """
    if isinstance(result, dict):
        issues = result.get("issues", [])
        return list(issues) if isinstance(issues, list) else []
    if isinstance(result, list):
        return result
    return []


def _iter_pages(client: TicketTransport, jql: str, page_size: int = 100, cap: int | None = None):
    """Generator yielding one page (list[dict]) per ACLI call.

    Termination:
      * Page is empty or shorter than ``page_size`` (natural end).
      * Accumulated issue count would meet/exceed the per-query ACLI
        ceiling — raises ``SilentTruncationError`` before yielding the
        violating page.
      * Caller-supplied ``cap`` is reached — stops cleanly (does NOT
        raise; the cap is an intentional client-side truncation, not a
        silent ACLI truncation). When set, the final yielded page is
        sliced so total yielded items never exceed ``cap``.
      * ACLI returns the same ``next_page_token`` on two consecutive calls
        ("same-token-twice") — raises
        ``SilentTruncationError(reason='same-token-twice')``.
      * The endpoint returns the same first issue at a new offset, i.e. it is
        ignoring ``startAt`` — raises
        ``SilentTruncationError(reason='offset-stall')``.

    Termination is on an EMPTY page only; a SHORT page does not end the scan,
    because Jira DC silently truncates ``maxResults`` above
    ``jira.search.views.default.max`` and a short page is therefore not proof of
    exhaustion (bug deac).
    """
    start_at = 0
    accumulated = 0
    prev_token: object = None
    token_seen_count = 0
    prev_first_key: object = None
    while True:
        result = client.search_issues(jql, start_at=start_at, max_results=page_size)
        page = _extract_issues(result)

        # Same-token-twice cursor-stall detection. Inspect any of the common
        # token attribute names exposed by the client (POSIX-ish duck-typing).
        cur_token = None
        for attr in ("next_page_token", "nextPageToken"):
            if hasattr(client, attr):
                cur_token = getattr(client, attr)
                break
        if cur_token is not None and prev_token is not None and cur_token == prev_token:
            token_seen_count += 1
            if token_seen_count >= 1:
                raise SilentTruncationError(
                    "ACLI returned the same next_page_token twice in a row "
                    "(same-token-twice cursor stall)",
                    reason="same-token-twice",
                )
        else:
            token_seen_count = 0
        prev_token = cur_token

        if not page:
            return

        # OFFSET-STALL DETECTION — must run BEFORE the yield, or the repeated page is
        # emitted to the caller and its issues are double-counted (observed: a
        # non-offset-aware client yielded its 3 issues twice, which drove duplicate
        # creates in a dry-run pass that must write nothing).
        #
        # Driving the loop off the RETURNED page (below) means a client that ignores
        # ``startAt`` re-serves the same page forever, so the repeat must be caught. What
        # to do about it depends on whether it could loop:
        #
        #   * repeated FULL page (len >= page_size) — the server claims there is more and
        #     keeps handing back the same items, so the loop never terminates. Returning
        #     what we have would be the silent partial this function exists to refuse:
        #     RAISE.
        #   * repeated SHORT page — the client returned fewer than asked for and then
        #     repeated itself, so it is not offset-aware and has nothing further to give.
        #     We already hold everything it can produce: stop cleanly.
        #
        # The short-page case does NOT reintroduce the DC truncation bug. A hardened DC
        # HONOURS ``startAt``, so its second capped page holds DIFFERENT issues and the
        # scan continues; only an offset-blind client repeats.
        first_key = page[0].get("key") if isinstance(page[0], dict) else None
        if first_key is not None and first_key == prev_first_key:
            if len(page) >= page_size:
                raise SilentTruncationError(
                    f"the search endpoint returned the same first issue ({first_key!r}) "
                    f"at a new offset ({start_at}) while reporting a full page — it is "
                    "ignoring startAt, so paging can never advance",
                    reason="offset-stall",
                )
            return
        prev_first_key = first_key

        # Per-query ACLI ceiling: if adding this page would reach or exceed
        # the ceiling, raise rather than yield a silently-truncated set.
        if accumulated + len(page) >= _ACLI_CEILING:
            raise SilentTruncationError(
                f"ACLI working set reached the {_ACLI_CEILING}-issue ceiling "
                "(JRACLOUD-94632 silent truncation)",
                reason="ceiling",
            )

        # Client-side cap: yield a clipped final page if we'd exceed `cap`.
        if cap is not None and accumulated + len(page) > cap:
            remaining = cap - accumulated
            if remaining > 0:
                yield page[:remaining]
            return

        yield page
        accumulated += len(page)

        if cap is not None and accumulated >= cap:
            return

        # Advance by what the server ACTUALLY returned, and continue until an EMPTY
        # page — never stop on a SHORT one.
        #
        # Jira DC silently truncates ``maxResults`` above
        # ``jira.search.views.default.max`` (a common hardening), so a short page is NOT
        # proof of exhaustion. The previous form (`return` on a short page, advance by
        # the REQUESTED page_size) therefore read a server-truncated FIRST page as the
        # final one and silently returned a partial snapshot. Measured against a client
        # serving 250 issues while capping pages at 20: 20 recovered, 92% lost, raising
        # nothing. Because ``collect()`` feeds ``_build_snapshot``, those are not missing
        # fields on a present issue — they are issues the pass never sees, each one a
        # candidate for the absence/deletion path. Identical defect and identical fix to
        # ``get_parent_map`` (bug deac; the transport sibling is 9263).
        #
        # (The offset-stall check runs above, BEFORE the yield — see there for why.)
        start_at += len(page)


def collect(
    client: TicketTransport, jql: str, page_size: int = 100, cap: int | None = None
) -> list[dict]:
    """Drain ``_iter_pages`` into a single flat list of issues."""
    issues: list[dict] = []
    for page in _iter_pages(client, jql, page_size=page_size, cap=cap):
        issues.extend(page)
    return issues


# --------------------------------------------------------------------------------------
# Partitioned drain — reconciliation capacity decoupled from project size (bug 5c5c)
# --------------------------------------------------------------------------------------
#
# The ceiling above is a VENDOR limit (JRACLOUD-94632) and it has now been reached twice:
# 1,000 -> 1,200 in bug f6cc, then again on 2026-09-05 when the REB active working set
# measured 1,388 (it was 1,050 at the 2026-05-26 probe). Raising the constant a third time
# only schedules the next outage, because a single query's capacity is pinned to how large
# the project has grown. ``collect_partitioned`` removes that coupling: it drains the query
# as a union of half-open ``created`` windows, so a slice is bounded by its WINDOW and the
# project may grow without limit.
#
# WHY ``created`` AND NOT ``issuekey``. Key ranges were the first candidate — keys are
# monotonic, so a window of width W bounds a slice at W issues. Measured against live Jira
# they are a SILENT-DROP TRAP: a relational ``issuekey`` comparison whose boundary key does
# not exist matches NOTHING and raises NOTHING.
#
#     project = REB AND statusCategory != "Done" AND issuekey < REB-5373  -> 1387  (exists)
#     project = REB AND statusCategory != "Done" AND issuekey < REB-5501  ->    0  (gap!)
#
# REB's max key is REB-5373 across 4,380 issues, so ~1,000 key slots are gaps left by
# deleted/moved issues. A 500-wide key tiling summed to 979 against a true 1,388 — 409
# issues lost in silence, which is precisely the defect this module exists to refuse.
# A ``created`` bound has no such precondition: ``created < X`` and ``created >= X``
# partition the space for ANY instant X, whether or not an issue was created at it. (The
# same string is handed to Jira on both sides of a split, so however Jira resolves its
# timezone it resolves both identically — the two halves stay exactly complementary.)
#
# ``created`` is also IMMUTABLE, which ``updated`` is not: an issue touched mid-pass would
# migrate across an ``updated`` boundary and be missed by both windows.
#
# HOW THE SEAM STAYS SAFE. Every slice is drained by ``_iter_pages`` UNCHANGED, so the
# cursor-stall, offset-stall and short-page-is-not-exhaustion defences run per slice exactly
# as before. Only ``reason == "ceiling"`` is ever intercepted, and only to re-ask for the
# SAME issues as two narrower windows that each return completely; ``same-token-twice`` and
# ``offset-stall`` always propagate. When a window can no longer be split the ceiling is
# re-raised, so the guard is preserved rather than weakened: this path never returns a
# partial slice, it only replaces one over-large complete read with several smaller ones.
#
# The split is structurally exhaustive: replacing [lo, hi) with [lo, mid) + [mid, hi) covers
# exactly the same instants with no gap and no overlap for any mid in (lo, hi). The OUTER
# edges are left unbounded (the leftmost window omits ``created >=`` and the rightmost omits
# ``created <``) so no sentinel date is ever assumed — including for issues created while
# the pass is running.

_JQL_DATE_FMT = "%Y-%m-%d %H:%M"

# Jira's JQL date literals resolve to minute precision, so a window narrower than a minute
# cannot be expressed and bisection bottoms out here.
_MIN_WINDOW = timedelta(minutes=1)

# First walk-back span for a window with no lower bound; doubles per level, so the number of
# probes is logarithmic in the project's age (REB, ~3 months old, needs one or two).
_INITIAL_SPAN = timedelta(days=30)

# Ceiling on that doubling. No Jira issue predates Jira, so a window this wide that STILL
# over-delivers means the server is not honouring the ``created`` predicate at all — the
# window-level sibling of the offset-stall check, where the server ignores ``startAt``.
# Narrowing further cannot help, so the drain stops walking and re-raises the ceiling rather
# than doubling until the date arithmetic overflows.
_MAX_SPAN = timedelta(days=365 * 40)

_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)

# JQL string literals, double- or single-quoted, honouring backslash escapes. Quoted text is
# blanked before the ORDER BY scan: a bare substring search would refuse a legitimate
# exhaustive query whose FIELD VALUE happens to contain the words — e.g.
# ``summary ~ "order by the numbers"`` — which is a filter, not a sort. An UNTERMINATED quote
# leaves its tail unblanked, so a malformed query still errs toward refusing.
_JQL_QUOTED_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")


def _has_order_by(jql: str) -> bool:
    """True when ``jql`` carries a real ORDER BY clause, ignoring quoted field values."""
    return bool(_ORDER_BY_RE.search(_JQL_QUOTED_RE.sub(" ", jql)))


class _Unsplittable(Exception):
    """A window is already at the finest expressible resolution."""


def _now() -> datetime:
    """The root pivot. A seam of its own, so tests can pin it.

    Only a heuristic: the split covers ``[lo, hi)`` exactly whatever instant is chosen, so
    a timezone disagreement with the Jira server costs an extra probe, never an issue.
    """
    return datetime.now(tz=UTC)


def _window_jql(base_jql: str, lo: datetime | None, hi: datetime | None) -> str:
    """``base_jql`` restricted to the half-open ``created`` window ``[lo, hi)``.

    An absent bound emits NO clause at all, so the outermost windows stay genuinely
    unbounded instead of relying on a sentinel date.
    """
    clauses = [base_jql]
    if lo is not None:
        clauses.append(f'created >= "{lo.strftime(_JQL_DATE_FMT)}"')
    if hi is not None:
        clauses.append(f'created < "{hi.strftime(_JQL_DATE_FMT)}"')
    return " AND ".join(clauses)


def _split_window(
    lo: datetime | None, hi: datetime | None, span: timedelta
) -> tuple[datetime, timedelta]:
    """Return the ``(mid, next_span)`` that bisects ``[lo, hi)``.

    ``[lo, mid)`` and ``[mid, hi)`` tile ``[lo, hi)`` exactly for any returned ``mid``, so
    coverage never depends on the choice — only the number of probes does. Raises
    ``_Unsplittable`` when the window is already at minute resolution.
    """
    if lo is not None and hi is not None:
        if hi - lo <= _MIN_WINDOW:
            raise _Unsplittable
        mid = lo + (hi - lo) / 2
        mid -= timedelta(seconds=mid.second, microseconds=mid.microsecond)
        if mid <= lo or mid >= hi:
            raise _Unsplittable
        return mid, span
    if span > _MAX_SPAN:
        # Walked past any plausible project history and the window still over-delivers:
        # the ``created`` predicate is being ignored. Refuse rather than narrow forever.
        raise _Unsplittable
    try:
        if hi is not None:  # unbounded below — walk the lower edge back geometrically
            return hi - span, span * 2
        if lo is not None:  # unbounded above — walk the upper edge forward
            return lo + span, span * 2
    except (OverflowError, OSError) as exc:  # ran off the representable date range
        raise _Unsplittable from exc
    return _now(), span  # the root: everything created so far, and everything after


def collect_partitioned(client: TicketTransport, jql: str, page_size: int = 100) -> list[dict]:
    """Drain ``jql`` EXHAUSTIVELY as a union of half-open ``created`` windows.

    Starts as the single unbounded query — identical to ``collect`` — and only subdivides a
    window that actually reaches the ACLI ceiling, so a project under the ceiling issues the
    exact same one query it does today. Windows are visited left to right, so the union is
    returned in ``created`` order.

    Raises:
        ValueError: ``jql`` carries an ``ORDER BY``. Such a query is a top-N read (see
            ``_DONE_RECENT_CAP``), and slicing it would silently change its meaning, so it
            is refused rather than mis-partitioned.
        SilentTruncationError: a cursor-stall or offset-stall in ANY slice, or a ceiling hit
            on a window that can no longer be narrowed.
    """
    if _has_order_by(jql):
        raise ValueError(
            "collect_partitioned cannot partition an ORDER BY query: ordering makes it a "
            f"top-N read, and slicing would change which issues it selects. Got: {jql!r}"
        )
    issues: list[dict] = []
    # LIFO of pending windows; children are pushed right-then-left so the union comes back
    # in ``created`` order.
    pending: list[tuple[datetime | None, datetime | None, timedelta]] = [
        (None, None, _INITIAL_SPAN)
    ]
    while pending:
        lo, hi, span = pending.pop()
        try:
            issues.extend(collect(client, _window_jql(jql, lo, hi), page_size=page_size))
        except SilentTruncationError as exc:
            # ONLY the ceiling is retryable, and only by asking for the same issues in two
            # narrower windows. A stalled cursor or a stalled offset means the slice itself
            # is untrustworthy: propagate, exactly as an unpartitioned drain would.
            if exc.reason != "ceiling":
                raise
            try:
                mid, next_span = _split_window(lo, hi, span)
            except _Unsplittable:
                raise exc from None
            pending.append((mid, hi, next_span))
            pending.append((lo, mid, next_span))
    return issues


def drain(
    client: TicketTransport, jql: str, page_size: int = 100, cap: int | None = None
) -> list[dict]:
    """Route ONE base query to the drain its shape requires.

    An UNCAPPED query is exhaustive — every matching issue must reach the snapshot — so it
    is partitioned by ``created`` window and its capacity stops depending on how large the
    project has grown (bug 5c5c). A CAPPED query is the ``ORDER BY updated DESC`` top-N read
    (``_DONE_RECENT_CAP``): partitioning would change WHICH issues it selects, so it stays
    on the single-query path, where the client-side cap returns long before the ceiling.
    """
    if cap is None:
        return collect_partitioned(client, jql, page_size=page_size)
    return collect(client, jql, page_size=page_size, cap=cap)
