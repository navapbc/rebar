"""RED tests for parent routing on the legacy update_one batch path.

Root cause (ticket 8b25-ae7a-efc3-47f6): the production outbound dispatch path
routes through ``applier.update_one`` (via ``_apply_batch``), whose field
allowlist ``_OUTBOUND_BATCH_ALLOWLIST`` did NOT include ``parent``. A
parent-only outbound update mutation (``fields={"parent": "DIG-X"}``) was
therefore stripped to an empty field set and ``client.set_parent`` was never
called — the parent never landed in Jira.

Because the parent never lands, the next fetch_snapshot still shows no parent,
the outbound differ re-emits the SAME parent mutation, and the bridge churns a
perpetual parent re-emission on every bound child (the Phase-6 idempotency
failure: ~230 steady-state ``fields=['parent']`` mutations).

The typed leaf ``_apply_outbound_update`` already routes parent via
``client.set_parent`` (applier.py ~316). These tests assert ``update_one``
does the same — pops ``parent`` from the field set and dispatches it through
``client.set_parent(issue_key, parent_key)`` instead of dropping it.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location(
        "applier_update_one_parent", APPLIER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_update_one_parent"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return _load_applier()


def test_update_one_routes_parent_to_set_parent(applier):
    """A parent-only update must call client.set_parent, NOT drop the field."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-100",
        "fields": {"parent": "DIG-EPIC-1"},
    }
    applier.update_one(mutation, client)
    client.set_parent.assert_called_once_with("DIG-100", "DIG-EPIC-1")


def test_update_one_does_not_pass_parent_to_update_issue(applier):
    """parent must never reach client.update_issue (ACLI edit can't reparent)."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-101",
        "fields": {"summary": "T", "parent": "DIG-EPIC-1"},
    }
    applier.update_one(mutation, client)
    # set_parent handles the reparent; update_issue gets summary only.
    client.set_parent.assert_called_once_with("DIG-101", "DIG-EPIC-1")
    if client.update_issue.called:
        _, kwargs = client.update_issue.call_args
        assert "parent" not in kwargs, (
            f"parent must not reach client.update_issue; got kwargs={kwargs!r}"
        )
        assert kwargs.get("summary") == "T"


def test_update_one_parent_400_is_non_fatal(applier):
    """A Jira HTTP 400 hierarchy rejection on set_parent must not raise."""
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = urllib.error.HTTPError(
        url="http://x", code=400, msg="bad request", hdrs=None, fp=None
    )
    mutation = {
        "action": "update",
        "key": "DIG-102",
        "fields": {"parent": "DIG-EPIC-1"},
    }
    # Must not raise — hierarchy rejection is a guarded warning.
    applier.update_one(mutation, client)
    client.set_parent.assert_called_once_with("DIG-102", "DIG-EPIC-1")


def test_update_one_no_parent_does_not_call_set_parent(applier):
    """When no parent field is present, set_parent must not be invoked."""
    client = MagicMock()
    client.update_issue.return_value = None
    mutation = {
        "action": "update",
        "key": "DIG-103",
        "fields": {"summary": "T"},
    }
    applier.update_one(mutation, client)
    client.set_parent.assert_not_called()
