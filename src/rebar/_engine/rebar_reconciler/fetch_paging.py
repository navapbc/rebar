"""ACLI pagination helpers extracted from ``fetcher``.

Holds the per-query pagination machinery: the ACLI ceiling constant, the
``SilentTruncationError`` raised on silent truncation, and the
``_iter_pages``/``collect`` generators that drain a paged ``search_issues``
result while guarding against ceiling, cursor-stall and offset-stall
truncation. Split out of ``fetcher`` (bug a33c) to keep that module under the
size cap; ``fetcher`` re-exports these names for its existing callers.
"""

from __future__ import annotations

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
