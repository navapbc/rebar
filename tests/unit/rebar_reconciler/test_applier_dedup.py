"""Tests for applier.py JQL dedup guard in create_one().

RED task f40b: verifies that:
  1. search_issues is called before create_issue on the miss path.
  2. Budget guard defers the mutation when rest_calls >= 200.
  3. On JQL hit, create_issue is NOT called and a dedup sentinel is returned.
  4. On JQL miss (empty results), create_issue IS called.

All tests mock AcliClient.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    """Load the applier module, failing all tests if absent."""
    if not APPLIER_PATH.exists():
        pytest.fail(
            f"applier.py not found at {APPLIER_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_applier()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_client(search_return=None):
    """Return a mock AcliClient whose method call order is tracked."""
    client = MagicMock()
    client.search_issues.return_value = search_return if search_return is not None else []
    client.create_issue.return_value = {"key": "DIG-999", "status": "created"}
    return client


def _make_create_mutation(local_id: str = "tick-0001") -> dict:
    """Return a minimal create mutation dict."""
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_called_before_create_on_miss(applier):
    """search_issues is called before create_issue on the miss path (empty JQL results)."""
    call_order: list[str] = []

    client = MagicMock()
    client.search_issues.side_effect = lambda *a, **kw: call_order.append("search") or []
    client.create_issue.side_effect = lambda *a, **kw: call_order.append("create") or {"key": "DIG-1"}

    mutation = _make_create_mutation("tick-0001")
    result = applier.create_one(mutation, client, rest_calls=0)

    assert call_order == ["search", "create"], (
        f"Expected search before create, got: {call_order}"
    )
    assert result is not None
    assert result.get("key") == "DIG-1"


def test_budget_guard_defers_at_200(applier):
    """Budget guard appends to deferred_creates and returns None when rest_calls >= 200."""
    client = _make_mock_client(search_return=[])
    deferred: list = []
    mutation = _make_create_mutation("tick-0002")

    result = applier.create_one(mutation, client, rest_calls=200, deferred_creates=deferred)

    assert result is None, "Expected None from budget-deferred create"
    assert mutation in deferred, "Mutation must be appended to deferred_creates"
    client.search_issues.assert_not_called()
    client.create_issue.assert_not_called()


def test_budget_guard_defers_above_200(applier):
    """Budget guard also defers when rest_calls > 200 (already over budget)."""
    client = _make_mock_client(search_return=[])
    deferred: list = []
    mutation = _make_create_mutation("tick-0003")

    result = applier.create_one(mutation, client, rest_calls=201, deferred_creates=deferred)

    assert result is None
    assert mutation in deferred
    client.search_issues.assert_not_called()
    client.create_issue.assert_not_called()


def test_budget_guard_does_not_defer_below_200(applier):
    """Budget guard allows create when rest_calls < 200."""
    client = _make_mock_client(search_return=[])
    deferred: list = []
    mutation = _make_create_mutation("tick-0004")

    applier.create_one(mutation, client, rest_calls=199, deferred_creates=deferred)

    assert deferred == [], "Mutation must NOT be deferred when rest_calls < 200"
    client.search_issues.assert_called_once()
    client.create_issue.assert_called_once()


def test_jql_hit_skips_create_issue(applier, tmp_path):
    """On JQL hit, create_issue is NOT called and dedup sentinel is returned."""
    existing_issue = {"key": "DIG-500", "fields": {"summary": "existing"}}
    client = _make_mock_client(search_return=[existing_issue])

    mutation = _make_create_mutation("tick-0005")
    result = applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    client.create_issue.assert_not_called()
    assert result is not None
    assert result.get("status") == "dedup-create-skipped"
    assert result.get("key") == "DIG-500"


def test_jql_miss_calls_create_issue(applier):
    """On JQL miss (empty search results), create_issue IS called with translated
    bridge schema (title from summary, ticket_type from issuetype)."""
    client = _make_mock_client(search_return=[])

    mutation = _make_create_mutation("tick-0006")
    result = applier.create_one(mutation, client, rest_calls=0)

    client.search_issues.assert_called_once()
    client.create_issue.assert_called_once()
    _call_args = client.create_issue.call_args
    _ticket_data = _call_args.args[0] if _call_args.args else _call_args.kwargs
    # title and ticket_type are required by AcliClient.create_issue;
    # the differ emits 'summary' / 'issuetype' which create_one translates.
    assert "title" in _ticket_data
    assert "ticket_type" in _ticket_data
    assert result is not None
    assert result.get("key") == "DIG-999"


def test_jql_query_uses_local_id_label(applier):
    """search_issues is called with the correct rebar-id label JQL for the mutation's local_id."""
    client = _make_mock_client(search_return=[])

    mutation = _make_create_mutation("tick-0007")
    applier.create_one(mutation, client, rest_calls=0)

    client.search_issues.assert_called_once_with('labels = "rebar-id:tick-0007"')
