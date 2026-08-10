from __future__ import annotations

from rebar.reducer.search import (
    _case_insensitive_span,
    project_search_result,
    search_result_to_llm,
)


def _state(**overrides: object) -> dict:
    state = {
        "ticket_id": "abcd-1234-ef56-7890",
        "alias": "kind-amber-otter",
        "title": "A ticket title",
        "ticket_type": "bug",
        "status": "open",
        "priority": 2,
        "description": None,
        "tags": [],
        "comments": [],
        "jira_key": None,
    }
    state.update(overrides)
    return state


def test_projection_normalizes_and_bounds_summary() -> None:
    result = project_search_result(
        _state(description="  First\n\nparagraph  " + "more words " * 40),
        "ticket",
    )

    assert result["summary"].startswith("First paragraph more words")
    assert len(result["summary"]) == 240
    assert result["summary"].endswith("…")


def test_projection_centers_case_preserving_snippet_on_first_positive_term() -> None:
    result = project_search_result(
        _state(description=("before " * 80) + "MixedCaseNeedle" + (" after" * 80)),
        "status:open mixedcaseneedle after",
    )

    assert "MixedCaseNeedle" in result["snippet"]
    assert len(result["snippet"]) <= 240
    assert result["snippet"].startswith("…")
    assert result["snippet"].endswith("…")


def test_projection_uses_field_order_and_supports_non_body_matches() -> None:
    tag = project_search_result(_state(tags=["TagOnlyNeedle"]), "tagonlyneedle")
    identifier = project_search_result(_state(), "kind-amber-otter")
    comment = project_search_result(
        _state(comments=[{"body": "CommentOnlyNeedle", "author": "agent"}]),
        "commentonlyneedle",
    )

    assert tag["snippet"] == "TagOnlyNeedle"
    assert identifier["snippet"] == "kind-amber-otter"
    assert comment["snippet"] == "CommentOnlyNeedle"


def test_case_insensitive_span_maps_lowercase_expansion_to_original_field() -> None:
    assert _case_insensitive_span("İ before Needle", "needle") == (9, 6)
    assert _case_insensitive_span("before İ after", "i̇") == (7, 1)


def test_projection_nulls_and_llm_omission() -> None:
    result = project_search_result(_state(description=" \n "), "status:open -noise")

    assert result["summary"] is None
    assert result["snippet"] is None
    assert search_result_to_llm(result) == {
        "id": "abcd-1234-ef56-7890",
        "a": "kind-amber-otter",
        "ttl": "A ticket title",
        "t": "bug",
        "st": "open",
        "pr": 2,
    }
