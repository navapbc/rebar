"""Unit tests for provider-neutral external-tracker stripping (P1.2)."""

from __future__ import annotations

import pytest

from rebar._io._strip import strip_external

pytestmark = pytest.mark.unit


def test_strips_bridge_alerts_and_comment_jira_id() -> None:
    state = {
        "ticket_id": "t1",
        "bridge_alerts": [{"uuid": "x", "reason": "drift"}],
        "comments": [
            {"body": "a", "author": "u", "jira_comment_id": "10001"},
            {"body": "b", "author": "u"},
        ],
    }
    out = strip_external(state)
    assert "bridge_alerts" not in out
    assert "jira_comment_id" not in out["comments"][0]
    assert out["comments"][1] == {"body": "b", "author": "u"}


def test_strips_top_level_provider_keys() -> None:
    out = strip_external({"ticket_id": "t1", "jira_key": "PROJ-7", "jira_url": "http://x"})
    assert "jira_key" not in out
    assert "jira_url" not in out
    assert out["ticket_id"] == "t1"


def test_preserves_provenance_and_is_non_mutating() -> None:
    state = {
        "ticket_id": "t1",
        "source_id": "old-1",
        "source_author": "orig",
        "bridge_alerts": [{"uuid": "x"}],
        "comments": [{"body": "a", "jira_comment_id": "1"}],
    }
    out = strip_external(state)
    # provenance survives (it is OUR metadata, not external linkage)
    assert out["source_id"] == "old-1"
    assert out["source_author"] == "orig"
    # original is untouched (deep copy)
    assert state["bridge_alerts"] == [{"uuid": "x"}]
    assert state["comments"][0]["jira_comment_id"] == "1"
