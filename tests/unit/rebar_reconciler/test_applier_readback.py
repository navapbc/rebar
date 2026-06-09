"""Integration readback test for create_one() identity marker round-trip.

Task eab4: verifies that after create_one() creates a Jira issue, the
write-then-read round-trip is correct:
  1. get_entity_property("DIG-999", "dso_local_id") returns the local ticket ID.
  2. One of the add_label calls included a label matching "rebar-id:<local_id>".

All tests use a mock AcliClient — no real Jira calls are made.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_readback", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_readback"] = mod
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
# Mock client factory
# ---------------------------------------------------------------------------


def _make_readback_client(issue_key: str = "DIG-999"):
    """Return a mock AcliClient that:

    - create_issue() returns {"key": issue_key}
    - search_issues() returns [] (no dedup hit)
    - add_label(key, label) stores (key, label) in call_log
    - set_entity_property(key, prop_name, value) stores (key, prop_name, value)
      in call_log AND persists the value for get_entity_property round-trip
    - get_entity_property(key, prop_name) returns the value stored via
      set_entity_property, or raises KeyError if not found
    """
    add_label_log: list[tuple[str, str]] = []
    entity_properties: dict[tuple[str, str], object] = {}
    set_entity_property_log: list[tuple[str, str, object]] = []

    class MockClient:
        def create_issue(self, fields):
            return {"key": issue_key}

        def search_issues(self, jql):
            return []

        def add_label(self, key: str, label: str) -> None:
            add_label_log.append((key, label))

        def set_entity_property(self, key: str, prop_name: str, value: object) -> None:
            set_entity_property_log.append((key, prop_name, value))
            entity_properties[(key, prop_name)] = value

        def get_entity_property(self, key: str, prop_name: str) -> object:
            stored = entity_properties.get((key, prop_name))
            if stored is None and (key, prop_name) not in entity_properties:
                raise KeyError(f"property {prop_name!r} not set for {key!r}")
            return stored

        @property
        def add_label_calls(self) -> list[tuple[str, str]]:
            return add_label_log

        @property
        def set_entity_property_calls(self) -> list[tuple[str, str, object]]:
            return set_entity_property_log

    return MockClient()


def _make_create_mutation(local_id: str) -> dict:
    """Return a minimal create mutation dict."""
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Readback integration tests
# ---------------------------------------------------------------------------


def test_get_entity_property_returns_local_id_after_create(applier, tmp_path):
    """Assert 1: get_entity_property round-trip returns the local_id written by create_one."""
    local_id = "tick-readback-001"
    issue_key = "DIG-999"
    client = _make_readback_client(issue_key)
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    stored_value = client.get_entity_property(issue_key, "dso_local_id")
    assert stored_value == local_id, (
        f"Expected get_entity_property({issue_key!r}, 'dso_local_id') == {local_id!r}, "
        f"got {stored_value!r}"
    )


def test_add_label_includes_rebar_id_label_after_create(applier, tmp_path):
    """Assert 2: one of the add_label calls contains a label matching rebar-id:<local_id>."""
    local_id = "tick-readback-002"
    issue_key = "DIG-999"
    client = _make_readback_client(issue_key)
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    expected_label = f"rebar-id:{local_id}"
    label_calls = client.add_label_calls
    labels_written = [label for (_key, label) in label_calls]
    assert expected_label in labels_written, (
        f"Expected label {expected_label!r} in add_label calls, "
        f"got labels: {labels_written}"
    )


def test_readback_both_assertions_together(applier, tmp_path):
    """Readback round-trip: both entity property and rebar-id label are correct after create."""
    local_id = "tick-readback-003"
    issue_key = "DIG-777"
    client = _make_readback_client(issue_key)
    mutation = _make_create_mutation(local_id)

    applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    # Assert 1: entity property readback
    stored_local_id = client.get_entity_property(issue_key, "dso_local_id")
    assert stored_local_id == local_id, (
        f"Entity property mismatch: expected {local_id!r}, got {stored_local_id!r}"
    )

    # Assert 2: rebar-id label present in add_label calls for the correct key
    expected_label = f"rebar-id:{local_id}"
    label_calls_for_key = [
        label for (key, label) in client.add_label_calls if key == issue_key
    ]
    assert expected_label in label_calls_for_key, (
        f"Expected label {expected_label!r} among labels for {issue_key!r}: "
        f"{label_calls_for_key}"
    )
