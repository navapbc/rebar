"""RED tests for the legacy update_one outbound field filter.

Historical bug (bug 85a1-f581-2252-4a21): the typed leaf ``_apply_outbound_update``
filters outbound update fields through ``_OUTBOUND_UPDATE_ALLOWLIST``, but
the actual production path on the outbound batch dispatcher routes through
``update_one`` (applier.py:1744), which was unfiltered. A local issuetype
change (probe Phase 2 ticket_type=task→bug) consequently flowed through as
``--issuetype Bug`` to ACLI's ``jira workitem edit``, which rejects the
flag with non-zero exit. ``_apply_batch`` had no try/except around this and
the exception propagated up, aborting the entire batch loop and silently
losing every subsequent outbound update (Phase 2 every-field FAIL).

These tests assert ``update_one`` strips disallowed fields BEFORE calling
``client.update_issue`` so the legacy batch path behaves like the typed
leaf with respect to the apply allowlist.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location(
        "applier_update_one_filter", APPLIER_PATH
    )
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
    """update_one must NOT pass issuetype to client.update_issue (ACLI rejects --issuetype on edit)."""
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
        assert f in kwargs, f"allowlisted field {f} must reach client.update_issue; kwargs={kwargs!r}"


def test_update_one_forwards_status_to_client(applier):
    """Bug 85a1 (Gap 8): status is now allowlisted and must be forwarded.

    Previously status was dropped here because outbound status was gated
    BY_DESIGN behind REBAR_RECONCILER_STATUS_GATING. Gap 8 removed that gate
    and rewrote ``transition_issue`` to use REST. update_one now passes
    status through to ``client.update_issue``, which routes it to
    ``transition_issue`` → REST POST /transitions.
    """
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
        f"status must reach client.update_issue (no BY_DESIGN drop); "
        f"got kwargs={kwargs!r}"
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
    """When all fields are stripped, update_issue is still called with empty kwargs.

    This is intentional: the legacy contract treats an empty changed_fields as
    a no-op success, and a fix in this area should not change that. Comment/label
    dispatch (lines 1788+) must still run after the call returns.
    """
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
