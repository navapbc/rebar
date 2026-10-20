"""Cloud search must stop network pagination when the consumer stops reading."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from rebar.reconciler.adapters.jira.acli import AcliClient
from rebar.reconciler.adapters.jira.acli_graph import RunawayPaginationError
from rebar.reconciler.fetch_paging import collect


@pytest.fixture
def client():
    # mechanism-ok: test_helper client — d730-8a25: exercises the Cloud search HTTP boundary.
    return AcliClient("https://example.atlassian.net", "user@example.com", "token")


def test_done_cap_bounds_actual_transport_reads(client, monkeypatch):
    requests = []
    jql = 'project = REB AND statusCategory = "Done" ORDER BY updated DESC'

    def post(path, body):
        requests.append((path, body))
        offset = int(body.get("nextPageToken", "0"))
        end = min(offset + body["maxResults"], 5276)
        return {
            "issues": [
                {"key": f"REB-{n}", "fields": {"labels": ["kept"]}} for n in range(offset, end)
            ],
            "nextPageToken": str(end),
            "isLast": end == 5276,
        }

    monkeypatch.setattr(client, "_direct_rest_post_json", post)
    monkeypatch.setattr(client, "_run", lambda *_a, **_k: pytest.fail("eager ACLI search"))
    issues = collect(client, jql, cap=1000)
    assert [issue["key"] for issue in issues] == [f"REB-{n}" for n in range(1000)]
    assert issues[-1]["fields"]["labels"] == ["kept"]
    assert len(requests) == 10
    assert all(path == "/rest/api/3/search/jql" for path, _ in requests)
    assert all(body["jql"] == jql and body["maxResults"] == 100 for _, body in requests)
    assert requests[0][1]["fields"] == [
        "issuetype",
        "key",
        "assignee",
        "priority",
        "status",
        "summary",
        "description",
        "labels",
    ]


@pytest.mark.parametrize(("start_at", "max_results"), [(-1, 50), (0, -1)])
def test_negative_slice_is_rejected_without_searching(client, monkeypatch, start_at, max_results):
    monkeypatch.setattr(
        client, "_direct_rest_post_json", lambda *_a: pytest.fail("unexpected read")
    )
    with pytest.raises(ValueError, match="non-negative"):
        client.search_issues("active", start_at=start_at, max_results=max_results)


def test_zero_limit_does_not_search(client, monkeypatch):
    monkeypatch.setattr(
        client, "_direct_rest_post_json", lambda *_a: pytest.fail("unexpected read")
    )
    assert client.search_issues("active", max_results=0) == []


def test_short_and_empty_pages_with_cursor_are_not_end_of_search(client, monkeypatch):
    answers = iter(
        [
            {"issues": [{"key": "REB-1"}], "nextPageToken": "a"},
            {"issues": [], "nextPageToken": "b"},
            {"issues": [{"key": "REB-2"}], "nextPageToken": "c"},
            {"issues": [{"key": "REB-3"}], "isLast": True},
        ]
    )
    calls = []

    def post(path, body):
        calls.append(body)
        return next(answers)

    monkeypatch.setattr(client, "_direct_rest_post_json", post)
    assert client.search_issues("active", max_results=2) == [{"key": "REB-1"}, {"key": "REB-2"}]
    assert len(calls) == 3
    assert client.search_issues("active", start_at=2, max_results=2) == [{"key": "REB-3"}]
    assert client.search_issues("active", start_at=3) == []
    assert len(calls) == 4
    assert client.search_issues("active", max_results=1) == [{"key": "REB-1"}]
    assert len(calls) == 4


def test_stalled_cursor_is_rejected_before_returning_its_page(client, monkeypatch):
    answers = iter(
        [
            {"issues": [{"key": "REB-1"}], "nextPageToken": "same"},
            {"issues": [{"key": "REB-2"}], "nextPageToken": "same"},
        ]
    )
    monkeypatch.setattr(client, "_direct_rest_post_json", lambda *_a: next(answers))
    assert client.search_issues("active", max_results=1) == [{"key": "REB-1"}]
    with pytest.raises(RunawayPaginationError):
        client.search_issues("active", start_at=1, max_results=1)


@pytest.mark.parametrize("bad", [[], {}, {"issues": None}, {"issues": ["invalid"]}])
def test_malformed_search_fails_loud_and_does_not_poison_cache(client, monkeypatch, bad):
    answers = iter([bad, {"issues": [{"key": "REB-1"}], "isLast": True}])
    monkeypatch.setattr(client, "_direct_rest_post_json", lambda *_a: next(answers))
    with pytest.raises(RuntimeError, match="search"):
        client.search_issues("active")
    assert client.search_issues("active") == [{"key": "REB-1"}]


def test_failed_later_page_invalidates_partial_result(client, monkeypatch):
    calls = []

    def post(path, body):
        calls.append(body)
        if len(calls) == 2:
            raise urllib.error.HTTPError(path, 429, "rate limited", {}, None)
        if len(calls) == 1:
            return {"issues": [{"key": "old"}], "nextPageToken": "a"}
        return {"issues": [{"key": "fresh"}], "isLast": True}

    monkeypatch.setattr(client, "_direct_rest_post_json", post)
    with pytest.raises(urllib.error.HTTPError):
        client.search_issues("active", max_results=2)
    assert client.search_issues("active") == [{"key": "fresh"}]
    assert "nextPageToken" not in calls[-1]


@pytest.mark.parametrize("invalidate_all", [False, True])
def test_invalidation_discards_buffered_issues_and_cursor(client, monkeypatch, invalidate_all):
    calls: list[dict[str, Any]] = []

    def post(path, body):
        calls.append(body)
        prefix = "old" if len(calls) == 1 else "new"
        return {"issues": [{"key": prefix + "-1"}, {"key": prefix + "-2"}], "nextPageToken": "a"}

    monkeypatch.setattr(client, "_direct_rest_post_json", post)
    assert client.search_issues("active", max_results=1) == [{"key": "old-1"}]
    client.invalidate_search_cache(None if invalidate_all else "active")
    assert client.search_issues("active", max_results=1) == [{"key": "new-1"}]
    assert "nextPageToken" not in calls[-1]


def test_offset_first_read_drains_to_requested_slice_and_reuses_earlier_results(
    client, monkeypatch
):
    calls = []

    def post(path, body):
        offset = int(body.get("nextPageToken", "0"))
        calls.append(body)
        return {"issues": [{"key": str(offset)}], "nextPageToken": str(offset + 1)}

    monkeypatch.setattr(client, "_direct_rest_post_json", post)
    assert client.search_issues("active", start_at=3, max_results=1) == [{"key": "3"}]
    assert len(calls) == 4
    assert client.search_issues("active", start_at=1, max_results=2) == [{"key": "1"}, {"key": "2"}]
    assert len(calls) == 4
