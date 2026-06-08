"""Unit tests for dso_reconciler/conflict_resolver.py.

Tests cover:
  - test_resolve_state_returns_local: local_val always wins.
  - test_resolve_additive_list_union: list union preserves order, no dupes.
  - test_resolve_additive_string_concat: string fields concatenated with separator.
  - test_resolve_additive_one_none: only one non-None value is returned.
  - test_resolve_set_valued_union: returns union of both sets.
  - test_resolve_field_status_dispatches_state: 'status' → resolve_state.
  - test_resolve_field_description_dispatches_additive: 'description' → resolve_additive.
  - test_resolve_field_labels_dispatches_set_valued: 'labels' → resolve_set_valued.
  - test_resolve_field_unknown_defaults_to_state: unknown field → resolve_state (local wins).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "conflict_resolver.py"
)


def _load_conflict_resolver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("conflict_resolver", CONFLICT_RESOLVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def cr() -> ModuleType:
    return _load_conflict_resolver()


# ---------------------------------------------------------------------------
# resolve_state tests
# ---------------------------------------------------------------------------


def test_resolve_state_returns_local(cr: ModuleType) -> None:
    """local_val always wins for state fields, regardless of remote_val."""
    assert cr.resolve_state("open", "closed") == "open"
    assert cr.resolve_state(None, "closed") is None
    assert cr.resolve_state("open", None) == "open"
    assert cr.resolve_state(42, 99) == 42


# ---------------------------------------------------------------------------
# resolve_additive tests
# ---------------------------------------------------------------------------


def test_resolve_additive_list_union_preserves_order_no_dupes(cr: ModuleType) -> None:
    """List union: items from local first, then remote items not already present."""
    result = cr.resolve_additive(["a", "b"], ["b", "c"])
    assert result == ["a", "b", "c"]


def test_resolve_additive_list_union_empty_remote(cr: ModuleType) -> None:
    """List union with empty remote returns local."""
    result = cr.resolve_additive(["x", "y"], [])
    assert result == ["x", "y"]


def test_resolve_additive_string_concat_appends_new_content(cr: ModuleType) -> None:
    """String fields: remote content appended when it is not already in local."""
    result = cr.resolve_additive("hello", "world")
    assert result == "hello\nworld"


def test_resolve_additive_string_no_duplicate_content(cr: ModuleType) -> None:
    """String fields: remote content not appended when already present in local."""
    result = cr.resolve_additive("hello world", "hello")
    assert result == "hello world"


def test_resolve_additive_string_empty_local(cr: ModuleType) -> None:
    """String fields: when local is empty, remote is returned."""
    result = cr.resolve_additive("", "world")
    assert result == "world"


def test_resolve_additive_local_none(cr: ModuleType) -> None:
    """When local is None, remote value is returned."""
    result = cr.resolve_additive(None, "value")
    assert result == "value"


def test_resolve_additive_remote_none(cr: ModuleType) -> None:
    """When remote is None, local value is returned."""
    result = cr.resolve_additive("value", None)
    assert result == "value"


def test_resolve_additive_both_none(cr: ModuleType) -> None:
    """When both are None, None is returned."""
    result = cr.resolve_additive(None, None)
    assert result is None


# ---------------------------------------------------------------------------
# resolve_set_valued tests
# ---------------------------------------------------------------------------


def test_resolve_set_valued_union(cr: ModuleType) -> None:
    """Returns the union of both sets."""
    result = cr.resolve_set_valued({"a", "b"}, {"b", "c"}, [])
    assert set(result) == {"a", "b", "c"}


def test_resolve_set_valued_empty_local(cr: ModuleType) -> None:
    """Empty local set — remote set is fully returned."""
    result = cr.resolve_set_valued(set(), {"x"}, [])
    assert set(result) == {"x"}


def test_resolve_set_valued_none_local(cr: ModuleType) -> None:
    """None local treated as empty set."""
    result = cr.resolve_set_valued(None, {"y"}, None)
    assert set(result) == {"y"}


def test_resolve_set_valued_empty_both(cr: ModuleType) -> None:
    """Both empty — result is empty."""
    result = cr.resolve_set_valued(set(), set(), [])
    assert result == [] or set(result) == set()


# ---------------------------------------------------------------------------
# resolve_field dispatch tests
# ---------------------------------------------------------------------------


def test_resolve_field_status_dispatches_to_state(cr: ModuleType) -> None:
    """'status' is a state field — local wins."""
    assert cr.resolve_field("status", "open", "closed", None) == "open"


def test_resolve_field_assignee_dispatches_to_state(cr: ModuleType) -> None:
    """'assignee' is a state field — local wins."""
    assert cr.resolve_field("assignee", "alice", "bob", None) == "alice"


def test_resolve_field_priority_dispatches_to_state(cr: ModuleType) -> None:
    """'priority' is a state field — local wins."""
    assert cr.resolve_field("priority", 1, 3, None) == 1


def test_resolve_field_description_dispatches_to_additive(cr: ModuleType) -> None:
    """'description' is an additive field — content merged."""
    result = cr.resolve_field("description", "local desc", "remote desc", None)
    assert "local desc" in result
    assert "remote desc" in result


def test_resolve_field_comments_dispatches_to_additive(cr: ModuleType) -> None:
    """'comments' is an additive field — list union applied."""
    result = cr.resolve_field("comments", ["c1"], ["c1", "c2"], None)
    assert set(result) == {"c1", "c2"}


def test_resolve_field_labels_dispatches_to_set_valued(cr: ModuleType) -> None:
    """'labels' is a set field — union of both."""
    result = cr.resolve_field("labels", {"bug"}, {"enhancement"}, [])
    assert set(result) == {"bug", "enhancement"}


def test_resolve_field_watchers_dispatches_to_set_valued(cr: ModuleType) -> None:
    """'watchers' is a set field — union of both."""
    result = cr.resolve_field("watchers", {"alice"}, {"bob"}, [])
    assert set(result) == {"alice", "bob"}


def test_resolve_field_unknown_defaults_to_state(cr: ModuleType) -> None:
    """Unknown field names default to resolve_state (local wins)."""
    assert cr.resolve_field("unknown_field", "local", "remote", None) == "local"
    assert cr.resolve_field("xyz_custom", 99, 0, None) == 99


def test_field_classes_registry_contains_expected_keys(cr: ModuleType) -> None:
    """FIELD_CLASSES registry has the expected field mappings."""
    fc = cr.FIELD_CLASSES
    assert fc["status"] == "state"
    assert fc["assignee"] == "state"
    assert fc["priority"] == "state"
    assert fc["description"] == "additive"
    assert fc["comments"] == "additive"
    assert fc["labels"] == "set"
    assert fc["watchers"] == "set"
    assert fc["links"] == "set"
