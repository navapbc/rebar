"""Assert the enrichment return shapes shared by FakeAcliClient and AcliClient.

The fake runs hermetically. The client runs in the opt-in external Jira tier. Checks require
keys and JSON primitive types without pinning values, so the two cannot drift silently.
"""

from __future__ import annotations

from typing import Any


def assert_search_shape(result: Any) -> None:
    """``search_issues`` -> list of ``{"key": str, "fields": dict, ...}``."""
    assert isinstance(result, list), f"search_issues must return a list, got {type(result)}"
    for issue in result:
        assert isinstance(issue, dict)
        assert isinstance(issue.get("key"), str) and issue["key"], "issue.key must be non-empty str"
        assert isinstance(issue.get("fields"), dict), "issue.fields must be a dict"


def assert_comment_map_shape(result: Any) -> None:
    """``get_comment_map`` -> ``{key: {"comments": [{"id", "body", ...}], ...}}``."""
    assert isinstance(result, dict), "get_comment_map must return a dict"
    for key, field in result.items():
        assert isinstance(key, str) and key
        assert isinstance(field, dict), "comment field must be a dict (the nested shape)"
        assert isinstance(field.get("comments"), list), "comment field must carry a 'comments' list"
        for c in field["comments"]:
            assert isinstance(c, dict)
            assert isinstance(c.get("id"), str), "comment.id must be a str"
            assert isinstance(c.get("body"), (dict, str)), "comment.body must be ADF dict or str"


def assert_issuelinks_map_shape(result: Any) -> None:
    """``get_issuelinks_map`` -> ``{key: [{"type": {"name": str}, in/outwardIssue?}]}``."""
    assert isinstance(result, dict), "get_issuelinks_map must return a dict"
    for key, links in result.items():
        assert isinstance(key, str) and key
        assert isinstance(links, list), "issuelinks value must be a list (possibly empty)"
        for lk in links:
            assert isinstance(lk, dict)
            assert isinstance(lk.get("type"), dict), "issuelink.type must be a dict"
            assert isinstance(lk["type"].get("name"), str) and lk["type"]["name"]
            for side in ("inwardIssue", "outwardIssue"):
                if side in lk:
                    assert isinstance(lk[side], dict)
                    assert isinstance(lk[side].get("key"), str), f"{side}.key must be a str"


def assert_parent_map_shape(result: Any) -> None:
    """``get_parent_map`` -> ``{key: parent_key_str | None}``."""
    assert isinstance(result, dict), "get_parent_map must return a dict"
    for key, parent in result.items():
        assert isinstance(key, str) and key
        assert parent is None or isinstance(parent, str), "parent must be a key str or None"
