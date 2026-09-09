"""Outbound update field filtering on the batch path.

``update_one`` forwards summary, description, priority, assignee, and status while
removing unsupported fields before ``update_issue``. An empty filtered update still
calls ``update_issue`` so later comment and label dispatch can continue.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_update_one_filter", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_update_one_filter"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return _load_applier()


def test_update_one_strips_issuetype(applier):
    """update_one must NOT pass issuetype to client.update_issue (ACLI rejects it on edit)."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-100",
        "fields": {
            "summary": "Updated title",
            "issuetype": "Bug",  # local ticket_type=bug — must be stripped
        },
    }
    applier.update_one(mutation, client)
    # update_issue should be called with summary but NOT issuetype.
    client.update_issue.assert_called_once()
    args, kwargs = client.update_issue.call_args
    assert args[0] == "DIG-100"
    assert "issuetype" not in kwargs, (
        f"issuetype must not reach client.update_issue; got kwargs={kwargs!r}"
    )
    assert kwargs.get("summary") == "Updated title"


def test_update_one_keeps_allowlisted_fields(applier):
    """update_one must pass summary, description, priority, assignee through."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-200",
        "fields": {
            "summary": "T",
            "description": "D",
            "priority": "Low",
            "assignee": "alice",
        },
    }
    applier.update_one(mutation, client)
    _, kwargs = client.update_issue.call_args
    for f in ("summary", "description", "priority", "assignee"):
        assert f in kwargs, (
            f"allowlisted field {f} must reach client.update_issue; kwargs={kwargs!r}"
        )


def test_update_one_forwards_status_to_client(applier):
    """Status is allowlisted and reaches the client's transition route."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-250",
        "fields": {"summary": "T", "status": "Blocked"},
    }
    applier.update_one(mutation, client)
    _, kwargs = client.update_issue.call_args
    assert kwargs.get("status") == "Blocked", (
        f"status must reach client.update_issue (no BY_DESIGN drop); got kwargs={kwargs!r}"
    )


def test_update_one_strips_unknown_fields(applier):
    """Unknown fields (not in the allowlist) are dropped, not forwarded."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-300",
        "fields": {
            "summary": "T",
            "totally_made_up_field": "x",
        },
    }
    applier.update_one(mutation, client)
    _, kwargs = client.update_issue.call_args
    assert "totally_made_up_field" not in kwargs
    assert kwargs.get("summary") == "T"


def test_update_one_empty_after_filter_still_calls_update_issue(applier):
    """An empty filtered field set still calls update_issue before later dispatch."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-400",
        "fields": {"issuetype": "Bug"},  # the only field — gets stripped
    }
    applier.update_one(mutation, client)
    # update_issue called with no fields kwargs (issuetype stripped).
    client.update_issue.assert_called_once()
    _, kwargs = client.update_issue.call_args
    assert "issuetype" not in kwargs
