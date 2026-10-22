"""Tests for identity marker writes in create_one() after Jira issue creation.

RED task 0089: verifies that after create_one() successfully creates a Jira issue,
it writes both:
  1. A ``rebar-id:<local_id>`` label via client.add_label().
  2. A ``local_id`` entity property via client.set_entity_property().

All tests mock AcliClient so no real Jira calls are made.
"""

from __future__ import annotations

import importlib
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler.create_route import _coordinated_create_core

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


def _load_applier():
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(scope="module")
def applier():
    """Load the applier module, failing all tests if absent."""
    if not APPLIER_PATH.exists():
        pytest.fail(
            f"applier.py not found at {APPLIER_PATH} — implement the module to make tests pass."
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


def test_set_entity_property_called_after_create(applier):
    """After create_one() creates an issue, set_entity_property is called with local_id."""
    local_id = "tick-abc1"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, create_core=_coordinated_create_core)

    client.set_entity_property.assert_called_once_with("DIG-999", "local_id", local_id)


def test_add_label_called_after_create(applier):
    """After create_one() creates an issue, add_label is called with rebar-id:<local_id>."""
    local_id = "tick-abc2"
    client = _make_mock_client(create_return={"key": "DIG-999"})
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, create_core=_coordinated_create_core)

    client.add_label.assert_called_once_with("DIG-999", f"rebar-id:{local_id}")


def test_identity_writes_use_correct_jira_key(applier):
    """Identity writes target the key returned by create_issue, not a hardcoded value."""
    local_id = "tick-abc3"
    client = _make_mock_client(create_return={"key": "PROJ-42"})
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, create_core=_coordinated_create_core)

    client.add_label.assert_called_once_with("PROJ-42", f"rebar-id:{local_id}")
    client.set_entity_property.assert_called_once_with("PROJ-42", "local_id", local_id)


def test_identity_writes_not_called_on_dedup_skip(applier, tmp_path):
    """When JQL dedup fires (issue already exists), identity writes are NOT called."""
    existing = {"key": "DIG-500", "fields": {"summary": "existing"}}
    client = _make_mock_client()
    client.search_issues.return_value = [existing]  # JQL hit
    mutation = _make_create_mutation("tick-abc4")

    applier.create_one(
        mutation, client, rest_calls=0, repo_root=tmp_path, create_core=_coordinated_create_core
    )

    client.add_label.assert_not_called()
    client.set_entity_property.assert_not_called()


def test_identity_writes_not_called_on_budget_defer(applier):
    """When budget guard fires (rest_calls >= 200), identity writes are NOT called."""
    client = _make_mock_client()
    mutation = _make_create_mutation("tick-abc5")

    result = applier.create_one(
        mutation, client, rest_calls=200, create_core=_coordinated_create_core
    )

    assert result is None
    client.add_label.assert_not_called()
    client.set_entity_property.assert_not_called()


def _bridge_alerts(tmp_path: Path, local_id: str) -> list[Path]:
    return list((tmp_path / ".tickets-tracker" / local_id).glob("*-BRIDGE_ALERT.json"))


def test_add_label_transient_5xx_is_one_attempt_no_rollback(applier, tmp_path):
    """ADR 0103 S3: a transient 5xx on add_label is NOT retried by the handler.

    The write-ahead create already recorded the key, so the label write is attempted
    exactly once, the original error propagates, the created issue is NOT deleted, and
    the identity alert is published for the next pass to retro-attach the label.
    """
    local_id = "tick-trans-5xx"
    client = _make_mock_client(create_return={"key": "DIG-555"})

    # The first add_label call raises 503; a second call would succeed, so a second
    # call would mean a hidden retry.
    err_503 = applier.JiraAPIError("Service Unavailable", status_code=503)
    client.add_label.side_effect = [err_503, None]
    client.set_entity_property.return_value = None

    mutation = _make_create_mutation(local_id)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(applier.JiraAPIError) as excinfo:
            applier.create_one(
                mutation,
                client,
                rest_calls=0,
                repo_root=tmp_path,
                create_core=_coordinated_create_core,
            )

    assert excinfo.value is err_503, "the original add_label error must propagate"
    assert client.add_label.call_count == 1, (
        f"add_label must be attempted once; got call_count={client.add_label.call_count}"
    )
    mock_sleep.assert_not_called()
    client.delete_issue.assert_not_called()
    client.set_entity_property.assert_not_called()
    assert len(_bridge_alerts(tmp_path, local_id)) == 1, "identity alert must be written"


def test_set_entity_property_transient_5xx_is_one_attempt_no_rollback(applier, tmp_path):
    """ADR 0103 S3 edge case 3: a 503 on set_entity_property after create is one call.

    The Cloud REST leg raises a raw ``HTTPError``. It is not retried: one call, the raw
    error propagates, no ``delete_issue``, and the identity alert is written.
    """
    local_id = "tick-trans-prop"
    client = _make_mock_client(create_return={"key": "DIG-556"})

    client.add_label.return_value = None
    err_503 = urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
    client.set_entity_property.side_effect = [err_503, None]

    mutation = _make_create_mutation(local_id)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            applier.create_one(
                mutation,
                client,
                rest_calls=0,
                repo_root=tmp_path,
                create_core=_coordinated_create_core,
            )

    assert excinfo.value is err_503, "the raw HTTPError must propagate unwrapped"
    assert client.set_entity_property.call_count == 1, (
        f"set_entity_property must be attempted once; got "
        f"call_count={client.set_entity_property.call_count}"
    )
    mock_sleep.assert_not_called()
    client.delete_issue.assert_not_called()
    assert len(_bridge_alerts(tmp_path, local_id)) == 1, "identity alert must be written"


def test_label_written_before_entity_property(applier):
    """add_label is called before set_entity_property (label first, then property)."""
    local_id = "tick-abc6"
    call_order: list[str] = []

    client = MagicMock()
    client.search_issues.return_value = []
    client.create_issue.return_value = {"key": "DIG-999"}
    client.add_label.side_effect = lambda *a, **kw: call_order.append("add_label")
    client.set_entity_property.side_effect = lambda *a, **kw: call_order.append(
        "set_entity_property"
    )

    mutation = _make_create_mutation(local_id)
    applier.create_one(mutation, client, rest_calls=0, create_core=_coordinated_create_core)

    assert call_order == ["add_label", "set_entity_property"], (
        f"Expected add_label before set_entity_property, got: {call_order}"
    )
