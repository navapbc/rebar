"""Failure injection tests for identity-write rollback path in create_one().

Edge-case tests for task e11e: injects a failure in set_entity_property() after
create() succeeds, and asserts that delete_issue() is called with the exact key
and that the original RuntimeError (not a different exception type) propagates.

These complement the broader rollback suite in test_applier_rollback.py with
two sharply-focused assertions:
  1. delete_issue is called with the exact key "DIG-999" (key precision).
  2. The raised exception is RuntimeError specifically — not a wrapper or subclass.
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
    spec = importlib.util.spec_from_file_location(
        "applier_failure_injection", APPLIER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_failure_injection"] = mod
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


def _make_mock_client(jira_key: str = "DIG-999"):
    """Return a mock client whose create_issue returns the given key."""
    client = MagicMock()
    client.search_issues.return_value = []  # JQL miss — proceed to create
    client.create_issue.return_value = {"key": jira_key}
    client.add_label.return_value = None
    return client


def _make_create_mutation(local_id: str = "tick-fi01") -> dict:
    """Return a minimal create mutation dict."""
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


def test_delete_issue_called_with_exact_key_on_set_entity_property_failure(applier, tmp_path):
    """delete_issue is called with the exact Jira key "DIG-999", not a wildcard."""
    client = _make_mock_client(jira_key="DIG-999")
    client.set_entity_property.side_effect = RuntimeError("property write failed")
    mutation = _make_create_mutation("tick-fi01")

    with pytest.raises(RuntimeError):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    # Precision check: must be called with the specific key returned by create_issue
    client.delete_issue.assert_called_once_with("DIG-999")


def test_runtime_error_type_preserved_when_set_entity_property_raises(applier, tmp_path):
    """The propagated exception is exactly RuntimeError, not a wrapper or other type."""
    client = _make_mock_client(jira_key="DIG-999")
    client.set_entity_property.side_effect = RuntimeError("property write failed")
    mutation = _make_create_mutation("tick-fi02")

    exc_info = None
    try:
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)
    except Exception as exc:
        exc_info = exc

    assert exc_info is not None, "expected an exception to be raised"
    # Must be exactly RuntimeError — not a subclass, wrapper, or different type
    assert type(exc_info) is RuntimeError, (
        f"expected RuntimeError, got {type(exc_info).__name__}: {exc_info}"
    )
