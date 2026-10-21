"""AcliClient per-JQL search memo + its supported freshness door (ticket 3288).

``AcliClient.search_issues`` memoizes its consumed results per JQL string in
``self._search_cache`` — no TTL, and negative (empty) answers are cached too.
That is correct for the pagination callers it was built for, but a caller that
re-asks the same JQL for freshness (the bug-d30c index-visibility poll class)
must evict first. These tests pin BOTH halves of that contract at the
``AcliClient._direct_rest_post_json`` HTTP seam, counting invocations:

- the memo itself (a second same-JQL search runs NO HTTP request, including a
  cached negative), and
- the supported door, ``invalidate_search_cache`` (per-JQL evict, full clear,
  and the never-searched no-op edge).
"""

from __future__ import annotations

from typing import Any

# The reconciler engine is on sys.path via the package conftest; import flat.
from rebar.reconciler.adapters.jira import acli as acli_mod


def _issue(key: str) -> dict[str, Any]:
    return {"key": key, "fields": {"summary": f"issue {key}"}}


class _CountingRunClient(acli_mod.AcliClient):
    """AcliClient whose ``_direct_rest_post_json`` seam is a scripted stub counting invocations.

    ``answers`` maps a JQL string to a LIST of successive result sets — each
    ``_direct_rest_post_json`` for that JQL consumes the next one (the last repeats), so a test
    can script a negative-to-positive flip across a cache eviction.
    """

    def __init__(self, answers: dict[str, list[list[dict[str, Any]]]]) -> None:
        super().__init__(
            "https://example.atlassian.net",
            "user@example.com",
            "token",
            jira_project="TEST",
        )
        self._answers = answers
        self.run_count: dict[str, int] = {}

    def _direct_rest_post_json(self, path: str, body: Any) -> Any:
        jql = body["jql"]
        n = self.run_count.get(jql, 0)
        self.run_count[jql] = n + 1
        scripted = self._answers[jql]
        result = scripted[min(n, len(scripted) - 1)]
        return {"issues": result, "isLast": True}


JQL_A = 'labels="rebar-id:aaaa"'
JQL_B = 'labels="rebar-id:bbbb"'


def test_same_jql_is_memoized_including_a_negative_first_answer() -> None:
    """Two same-JQL searches run ONE HTTP request; a cached negative stays cached."""
    client = _CountingRunClient({JQL_A: [[], [_issue("TEST-1")]]})

    first = client.search_issues(JQL_A)
    second = client.search_issues(JQL_A)

    assert first == []
    # The flip to a non-empty answer is scripted but must NOT be visible: the
    # negative first answer is memoized and replayed without an HTTP request.
    assert second == []
    assert client.run_count == {JQL_A: 1}


def test_invalidate_one_jql_makes_a_negative_to_positive_flip_visible() -> None:
    """The freshness door: evict, re-search, and the refreshed answer arrives."""
    client = _CountingRunClient({JQL_A: [[], [_issue("TEST-1")]]})

    assert client.search_issues(JQL_A) == []
    client.invalidate_search_cache(JQL_A)
    refreshed = client.search_issues(JQL_A)

    assert refreshed == [_issue("TEST-1")]
    assert client.run_count == {JQL_A: 2}


def test_invalidate_all_clears_every_jql_entry() -> None:
    """``invalidate_search_cache()`` with no argument empties the whole memo."""
    client = _CountingRunClient(
        {
            JQL_A: [[_issue("TEST-1")]],
            JQL_B: [[_issue("TEST-2")]],
        }
    )
    client.search_issues(JQL_A)
    client.search_issues(JQL_B)

    client.invalidate_search_cache()
    client.search_issues(JQL_A)
    client.search_issues(JQL_B)

    assert client.run_count == {JQL_A: 2, JQL_B: 2}


def test_invalidating_a_never_searched_jql_is_a_noop() -> None:
    """Evicting an absent entry neither raises nor disturbs other entries."""
    client = _CountingRunClient({JQL_A: [[_issue("TEST-1")]]})
    client.search_issues(JQL_A)

    client.invalidate_search_cache(JQL_B)  # never searched — must not raise

    assert client.search_issues(JQL_A) == [_issue("TEST-1")]
    assert client.run_count == {JQL_A: 1}  # A's memo entry survived
