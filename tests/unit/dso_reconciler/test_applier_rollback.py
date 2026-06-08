"""Tests for rollback logic in create_one() when identity writes fail.

RED task e635: verifies that if set_entity_property() or add_label() raises after
a Jira issue has been created, create_one() calls delete_issue(jira_key) to roll
back the orphaned issue and then re-raises the original exception.
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
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_rollback", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_rollback"] = mod
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


def _make_mock_client(create_return=None):
    """Return a mock AcliClient with create_issue returning a known key."""
    client = MagicMock()
    client.search_issues.return_value = []  # JQL miss — proceed to create
    client.create_issue.return_value = (
        create_return if create_return is not None else {"key": "DIG-999"}
    )
    return client


def _make_create_mutation(local_id: str = "tick-rb01") -> dict:
    """Return a minimal create mutation dict."""
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_delete_issue_called_when_set_entity_property_raises(applier, tmp_path):
    """When set_entity_property raises, delete_issue is called once to roll back."""
    local_id = "tick-rb01"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    client.add_label.return_value = None
    client.set_entity_property.side_effect = RuntimeError("property write failed")
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError, match="property write failed"):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    client.delete_issue.assert_called_once_with("DIG-999")


def test_original_exception_propagates_when_set_entity_property_raises(applier, tmp_path):
    """The original RuntimeError from set_entity_property propagates to the caller."""
    local_id = "tick-rb02"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    client.add_label.return_value = None
    original_error = RuntimeError("property write failed")
    client.set_entity_property.side_effect = original_error
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError) as exc_info:
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    assert exc_info.value is original_error


def test_delete_issue_called_when_add_label_raises(applier, tmp_path):
    """When add_label raises, delete_issue is called once to roll back."""
    local_id = "tick-rb03"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    label_error = RuntimeError("label write failed")
    client.add_label.side_effect = label_error
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError, match="label write failed"):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    client.delete_issue.assert_called_once_with("DIG-999")


def test_original_exception_propagates_when_delete_issue_also_raises(applier, tmp_path):
    """If delete_issue itself raises during rollback, the ORIGINAL exception still propagates."""
    local_id = "tick-rb04"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    client.add_label.return_value = None
    original_error = RuntimeError("property write failed")
    client.set_entity_property.side_effect = original_error
    client.delete_issue.side_effect = RuntimeError("delete also failed")
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError) as exc_info:
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    # Must be the original error, not the rollback error
    assert exc_info.value is original_error


def test_delete_issue_not_called_on_successful_identity_writes(applier, tmp_path):
    """When both identity writes succeed, delete_issue is never called."""
    local_id = "tick-rb05"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    client.add_label.return_value = None
    client.set_entity_property.return_value = None
    mutation = _make_create_mutation(local_id)

    result = applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    client.delete_issue.assert_not_called()
    assert result == {"key": "DIG-999"}
