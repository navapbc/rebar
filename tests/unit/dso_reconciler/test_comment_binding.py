"""Tests for dso_reconciler/comment_binding.py — comment identity matching.

Tests assert behavioral contracts for:
  - match_comments: binding local comments to Jira comments by ID
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (importlib convention per conftest.py docs)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
CB_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "comment_binding.py"
)


def _load_comment_binding() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comment_binding", CB_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Cannot load comment_binding module from {CB_PATH}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def cb_mod() -> ModuleType:
    return _load_comment_binding()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllBound:
    """Every local comment has a jira_comment_id matching a Jira comment."""

    def test_all_bound(self, cb_mod):
        local = [
            {"body": "local 1", "jira_comment_id": "100"},
            {"body": "local 2", "jira_comment_id": "200"},
        ]
        jira = [
            {"id": "100", "body": "jira 1"},
            {"id": "200", "body": "jira 2"},
        ]
        result = cb_mod.match_comments(local, jira)
        assert result["bound"] == [(0, 0), (1, 1)]
        assert result["local_only"] == []
        assert result["jira_only"] == []


class TestLocalOnly:
    """Local comments without jira_comment_id are outbound create candidates."""

    def test_local_only(self, cb_mod):
        local = [
            {"body": "new comment", "jira_comment_id": None},
            {"body": "another new", "jira_comment_id": None},
        ]
        jira = [
            {"id": "100", "body": "existing jira comment"},
        ]
        result = cb_mod.match_comments(local, jira)
        assert result["bound"] == []
        assert result["local_only"] == [0, 1]
        assert result["jira_only"] == [0]

    def test_missing_jira_comment_id_key(self, cb_mod):
        """Local comment that lacks the key entirely treated as unbound."""
        local = [{"body": "no binding key"}]
        jira = []
        result = cb_mod.match_comments(local, jira)
        assert result["local_only"] == [0]


class TestJiraOnly:
    """Jira comments not bound to any local comment are inbound create candidates."""

    def test_jira_only(self, cb_mod):
        local = []
        jira = [
            {"id": "100", "body": "jira comment 1"},
            {"id": "200", "body": "jira comment 2"},
        ]
        result = cb_mod.match_comments(local, jira)
        assert result["bound"] == []
        assert result["local_only"] == []
        assert result["jira_only"] == [0, 1]


class TestMixed:
    """Combination of bound + unbound on both sides."""

    def test_mixed(self, cb_mod):
        local = [
            {"body": "bound local", "jira_comment_id": "100"},
            {"body": "unbound local", "jira_comment_id": None},
            {"body": "another bound", "jira_comment_id": "300"},
        ]
        jira = [
            {"id": "100", "body": "jira 100"},
            {"id": "200", "body": "jira 200"},
            {"id": "300", "body": "jira 300"},
        ]
        result = cb_mod.match_comments(local, jira)
        assert result["bound"] == [(0, 0), (2, 2)]
        assert result["local_only"] == [1]
        assert result["jira_only"] == [1]

    def test_stale_binding(self, cb_mod):
        """Local comment bound to a Jira ID that no longer exists."""
        local = [
            {"body": "stale", "jira_comment_id": "999"},
        ]
        jira = [
            {"id": "100", "body": "exists"},
        ]
        result = cb_mod.match_comments(local, jira)
        # Stale binding -> local_only (caller decides tombstone vs re-create)
        assert result["bound"] == []
        assert result["local_only"] == [0]
        assert result["jira_only"] == [0]

    def test_integer_id_coercion(self, cb_mod):
        """Jira IDs may come as int; binding should still match via str coercion."""
        local = [{"body": "x", "jira_comment_id": "42"}]
        jira = [{"id": 42, "body": "y"}]
        result = cb_mod.match_comments(local, jira)
        assert result["bound"] == [(0, 0)]
