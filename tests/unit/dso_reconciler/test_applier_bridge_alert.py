"""Tests: BRIDGE_ALERT emitted on identity-write rollback in create_one().

Verifies that when set_entity_property() raises after a Jira issue has been
created, create_one() writes exactly one BRIDGE_ALERT event file tagged
'create-identity-write-failed' to the .tickets-tracker/<local_id>/ directory.

Story f2ed-be2b — Gap 2 coverage.
"""

from __future__ import annotations

import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location(
        "applier_bridge_alert", APPLIER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_bridge_alert"] = mod
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


def _make_mock_client():
    """Return a mock AcliClient configured for set_entity_property failure."""
    client = MagicMock()
    client.search_issues.return_value = []  # JQL miss — proceed to create
    client.create_issue.return_value = {"key": "DIG-999"}
    client.add_label.return_value = None
    client.set_entity_property.side_effect = RuntimeError("property write failed")
    return client


def _make_create_mutation(local_id: str) -> dict:
    """Return a minimal create mutation dict."""
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bridge_alert_emitted_on_set_entity_property_failure(applier, tmp_path):
    """Exactly one BRIDGE_ALERT file is written when set_entity_property raises."""
    local_id = "tick-alert01"
    client = _make_mock_client()
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError, match="property write failed"):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    alert_files = list(
        (tmp_path / ".tickets-tracker" / local_id).glob("*-BRIDGE_ALERT.json")
    )
    assert len(alert_files) == 1, (
        f"Expected exactly 1 BRIDGE_ALERT file, found {len(alert_files)}: {alert_files}"
    )


def test_bridge_alert_tag_is_create_identity_write_failed(applier, tmp_path):
    """The BRIDGE_ALERT event carries tag 'create-identity-write-failed' in data."""
    local_id = "tick-alert02"
    client = _make_mock_client()
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    alert_files = list(
        (tmp_path / ".tickets-tracker" / local_id).glob("*-BRIDGE_ALERT.json")
    )
    assert len(alert_files) == 1
    payload = json.loads(alert_files[0].read_text())
    data = payload.get("data", {})
    assert data.get("tag") == "create-identity-write-failed" or (
        "identity-write" in data.get("reason", "")
    ), f"BRIDGE_ALERT data does not contain expected tag/reason: {data}"


def test_bridge_alert_emitted_on_add_label_failure(applier, tmp_path):
    """Exactly one BRIDGE_ALERT file is written when add_label raises."""
    local_id = "tick-alert03"
    client = MagicMock()
    client.search_issues.return_value = []
    client.create_issue.return_value = {"key": "DIG-999"}
    client.add_label.side_effect = RuntimeError("label write failed")
    mutation = _make_create_mutation(local_id)

    with pytest.raises(RuntimeError, match="label write failed"):
        applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    alert_files = list(
        (tmp_path / ".tickets-tracker" / local_id).glob("*-BRIDGE_ALERT.json")
    )
    assert len(alert_files) == 1, (
        f"Expected exactly 1 BRIDGE_ALERT file, found {len(alert_files)}: {alert_files}"
    )


def test_no_bridge_alert_on_successful_identity_writes(applier, tmp_path):
    """No BRIDGE_ALERT file is written when identity writes succeed."""
    local_id = "tick-alert04"
    client = MagicMock()
    client.search_issues.return_value = []
    client.create_issue.return_value = {"key": "DIG-999"}
    client.add_label.return_value = None
    client.set_entity_property.return_value = None
    mutation = _make_create_mutation(local_id)

    result = applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    assert result == {"key": "DIG-999"}
    tracker_dir = tmp_path / ".tickets-tracker" / local_id
    if tracker_dir.exists():
        alert_files = list(tracker_dir.glob("*-BRIDGE_ALERT.json"))
        assert len(alert_files) == 0, (
            f"Expected no BRIDGE_ALERT files on success, found: {alert_files}"
        )
