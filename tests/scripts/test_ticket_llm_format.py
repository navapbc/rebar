"""Unit tests for ticket_reducer.llm_format.

Covers to_llm(), shorten_comment(), and shorten_dep() in isolation.

Test: python3 -m pytest tests/scripts/test_ticket_llm_format.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/rebar/_engine/ is on sys.path so that `from ticket_reducer...` resolves.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ticket_reducer.llm_format import to_llm, shorten_comment, shorten_dep  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture shim — provides a 'mod' namespace with the three public functions
# so existing test methods that call mod.to_llm() etc. work unchanged.
# ---------------------------------------------------------------------------


class _Mod:
    to_llm = staticmethod(to_llm)
    shorten_comment = staticmethod(shorten_comment)
    shorten_dep = staticmethod(shorten_dep)


@pytest.fixture(scope="module")
def mod() -> _Mod:
    return _Mod()


# ---------------------------------------------------------------------------
# to_llm — key mapping
# ---------------------------------------------------------------------------


class TestToLlmKeyMapping:
    def test_ticket_id_mapped_to_id(self, mod):
        result = mod.to_llm({"ticket_id": "abc-123"})
        assert "id" in result
        assert result["id"] == "abc-123"
        assert "ticket_id" not in result

    def test_ticket_type_mapped_to_t(self, mod):
        result = mod.to_llm({"ticket_type": "story"})
        assert result.get("t") == "story"

    def test_title_mapped_to_ttl(self, mod):
        result = mod.to_llm({"title": "My title"})
        assert result.get("ttl") == "My title"

    def test_status_mapped_to_st(self, mod):
        result = mod.to_llm({"status": "open"})
        assert result.get("st") == "open"

    def test_author_mapped_to_au(self, mod):
        result = mod.to_llm({"author": "alice"})
        assert result.get("au") == "alice"

    def test_parent_id_mapped_to_pid(self, mod):
        result = mod.to_llm({"parent_id": "epic-1"})
        assert result.get("pid") == "epic-1"

    def test_comments_mapped_to_cm(self, mod):
        result = mod.to_llm({"comments": [{"body": "hi", "author": "bob"}]})
        assert "cm" in result
        assert "comments" not in result

    def test_deps_mapped_to_dp(self, mod):
        result = mod.to_llm({"deps": [{"target_id": "t-1", "relation": "blocks"}]})
        assert "dp" in result
        assert "deps" not in result

    def test_conflicts_mapped_to_cf(self, mod):
        result = mod.to_llm({"conflicts": ["file.py"]})
        assert result.get("cf") == ["file.py"]

    def test_unknown_keys_passed_through_unchanged(self, mod):
        result = mod.to_llm({"custom_field": "hello", "extra_data": 42})
        assert result.get("custom_field") == "hello"
        assert result.get("extra_data") == 42


# ---------------------------------------------------------------------------
# to_llm — None values and empty lists omitted
# ---------------------------------------------------------------------------


class TestToLlmOmissions:
    def test_none_values_omitted(self, mod):
        result = mod.to_llm({"title": "x", "assignee": None})
        assert "asn" not in result
        assert "assignee" not in result

    def test_parent_id_none_omitted(self, mod):
        result = mod.to_llm({"title": "x", "parent_id": None})
        assert "pid" not in result

    def test_empty_list_omitted(self, mod):
        result = mod.to_llm({"title": "x", "comments": [], "deps": []})
        assert "cm" not in result
        assert "dp" not in result

    def test_non_empty_list_kept(self, mod):
        result = mod.to_llm({"comments": [{"body": "note", "author": "x"}]})
        assert "cm" in result

    def test_omit_keys_created_at_dropped(self, mod):
        result = mod.to_llm({"title": "t", "created_at": "2026-01-01T00:00:00Z"})
        assert "created_at" not in result

    def test_omit_keys_env_id_dropped(self, mod):
        result = mod.to_llm({"title": "t", "env_id": "prod"})
        assert "env_id" not in result

    def test_full_ticket_state(self, mod):
        state = {
            "ticket_id": "dso-001",
            "ticket_type": "story",
            "title": "As a user, I can do things",
            "status": "open",
            "author": "alice",
            "parent_id": None,
            "created_at": "2026-01-01T00:00:00Z",
            "env_id": "local",
            "comments": [],
            "deps": [],
        }
        result = mod.to_llm(state)
        assert result["id"] == "dso-001"
        assert result["t"] == "story"
        assert result["ttl"] == "As a user, I can do things"
        assert result["st"] == "open"
        assert result["au"] == "alice"
        assert "pid" not in result
        assert "created_at" not in result
        assert "env_id" not in result
        assert "cm" not in result
        assert "dp" not in result


# ---------------------------------------------------------------------------
# shorten_comment
# ---------------------------------------------------------------------------


class TestShortenComment:
    def test_body_mapped_to_b(self, mod):
        result = mod.shorten_comment({"body": "hello", "author": "x"})
        assert result.get("b") == "hello"
        assert "body" not in result

    def test_author_mapped_to_au(self, mod):
        result = mod.shorten_comment({"body": "hi", "author": "bob"})
        assert result.get("au") == "bob"

    def test_timestamp_omitted(self, mod):
        result = mod.shorten_comment(
            {"body": "hi", "author": "x", "timestamp": "2026-01-01T00:00:00Z"}
        )
        assert "timestamp" not in result

    def test_none_value_in_comment_omitted(self, mod):
        result = mod.shorten_comment({"body": "hi", "author": None})
        assert "au" not in result

    def test_non_dict_passthrough(self, mod):
        assert mod.shorten_comment("not a dict") == "not a dict"

    def test_unknown_comment_keys_passed_through(self, mod):
        result = mod.shorten_comment({"body": "hi", "author": "x", "extra": "val"})
        assert result.get("extra") == "val"


# ---------------------------------------------------------------------------
# shorten_dep
# ---------------------------------------------------------------------------


class TestShortenDep:
    def test_target_id_mapped_to_tid(self, mod):
        result = mod.shorten_dep({"target_id": "t-2", "relation": "blocks"})
        assert result.get("tid") == "t-2"
        assert "target_id" not in result

    def test_relation_mapped_to_r(self, mod):
        result = mod.shorten_dep({"target_id": "t-2", "relation": "blocks"})
        assert result.get("r") == "blocks"

    def test_link_uuid_omitted(self, mod):
        result = mod.shorten_dep(
            {"target_id": "t-2", "relation": "blocks", "link_uuid": "abc-uuid"}
        )
        assert "link_uuid" not in result

    def test_none_value_in_dep_omitted(self, mod):
        result = mod.shorten_dep({"target_id": "t-2", "relation": None})
        assert "r" not in result

    def test_non_dict_passthrough(self, mod):
        assert mod.shorten_dep(42) == 42

    def test_unknown_dep_keys_passed_through(self, mod):
        result = mod.shorten_dep(
            {"target_id": "t-2", "relation": "blocks", "meta": "info"}
        )
        assert result.get("meta") == "info"


# ---------------------------------------------------------------------------
# to_llm — comment and dep sub-key shortening applied via to_llm
# ---------------------------------------------------------------------------


class TestToLlmSubkeyShortening:
    def test_comments_subkeys_shortened(self, mod):
        result = mod.to_llm(
            {
                "comments": [
                    {"body": "lgtm", "author": "alice", "timestamp": "2026-01-01"}
                ]
            }
        )
        cm = result["cm"]
        assert len(cm) == 1
        assert cm[0].get("b") == "lgtm"
        assert cm[0].get("au") == "alice"
        assert "timestamp" not in cm[0]

    def test_deps_subkeys_shortened(self, mod):
        result = mod.to_llm(
            {
                "deps": [
                    {
                        "target_id": "dso-002",
                        "relation": "blocks",
                        "link_uuid": "uuid-xyz",
                    }
                ]
            }
        )
        dp = result["dp"]
        assert len(dp) == 1
        assert dp[0].get("tid") == "dso-002"
        assert dp[0].get("r") == "blocks"
        assert "link_uuid" not in dp[0]


# ---------------------------------------------------------------------------
# RED tests: priority and assignee get dedicated short keys
# ---------------------------------------------------------------------------


class TestPriorityAssigneeMapping:
    def test_priority_mapped_to_pr(self, mod):
        result = mod.to_llm({"priority": 2})
        assert result.get("pr") == 2

    def test_assignee_mapped_to_asn(self, mod):
        result = mod.to_llm({"assignee": "Joe"})
        assert result.get("asn") == "Joe"

    def test_description_mapped_to_desc(self, mod):
        result = mod.to_llm({"description": "A short description"})
        assert result.get("desc") == "A short description"
        assert "description" not in result
