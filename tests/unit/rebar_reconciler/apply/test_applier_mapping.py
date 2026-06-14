"""Tests for applier.py: mapping.json persistence and event emission on JQL hit.

RED task 9749: verifies that when create_one() encounters a JQL hit:
  1. mapping.json is written with the correct local_id -> jira_key entry.
  2. The manifest produced by apply() contains a dedup-create-skipped event.
  3. The mapping.json write is atomic (uses temp-file + rename, never partially visible).

All tests mock AcliClient.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_mapping_tests", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_mapping_tests"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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


def _make_mock_client(search_return=None, create_return=None):
    """Return a mock AcliClient."""
    client = MagicMock()
    client.search_issues.return_value = search_return if search_return is not None else []
    client.create_issue.return_value = (
        create_return if create_return is not None else {"key": "DIG-999", "status": "created"}
    )
    return client


def _make_create_mutation(local_id: str = "tick-abc1") -> dict:
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ---------------------------------------------------------------------------
# Tests: mapping.json written on JQL hit
# ---------------------------------------------------------------------------


def test_mapping_json_written_on_jql_hit(applier, tmp_path):
    """When JQL returns a hit, mapping.json is written with local_id -> jira_key."""
    existing_issue = {"key": "DIG-42", "fields": {}}
    client = _make_mock_client(search_return=[existing_issue])

    mutation = _make_create_mutation("tick-xyz1")
    events: list = []

    applier.create_one(
        mutation,
        client,
        rest_calls=0,
        events_list=events,
        repo_root=tmp_path,
    )

    mapping_path = tmp_path / "bridge_state" / "mapping.json"
    assert mapping_path.exists(), "mapping.json should be created on JQL hit"
    data = json.loads(mapping_path.read_text())
    assert data.get("tick-xyz1") == "DIG-42", (
        f"Expected mapping['tick-xyz1'] == 'DIG-42', got {data!r}"
    )


def test_mapping_json_preserves_existing_entries(applier, tmp_path):
    """Existing entries in mapping.json are preserved when a new hit is added."""
    mapping_path = tmp_path / "bridge_state" / "mapping.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps({"old-id": "DIG-1"}))

    existing_issue = {"key": "DIG-99", "fields": {}}
    client = _make_mock_client(search_return=[existing_issue])

    mutation = _make_create_mutation("new-id")
    applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    data = json.loads(mapping_path.read_text())
    assert data.get("old-id") == "DIG-1", "Pre-existing entry should be preserved"
    assert data.get("new-id") == "DIG-99", "New hit entry should be added"


def test_mapping_json_not_written_on_jql_miss(applier, tmp_path):
    """When JQL returns no hit, mapping.json is NOT created."""
    client = _make_mock_client(search_return=[])

    mutation = _make_create_mutation("tick-miss1")
    applier.create_one(mutation, client, rest_calls=0, repo_root=tmp_path)

    mapping_path = tmp_path / "bridge_state" / "mapping.json"
    assert not mapping_path.exists(), "mapping.json should NOT be created on JQL miss"


# ---------------------------------------------------------------------------
# Helpers: mock concurrency module for apply() tests
# ---------------------------------------------------------------------------


def _make_mock_concurrency():
    """Return a mock concurrency module with stable snapshot_head and rebase_retry."""
    mock_concurrency = MagicMock()
    # Always return the same HEAD SHA so no drift is detected
    mock_concurrency.snapshot_head.return_value = "abc1234def567890" * 2  # 32-char sha
    # rebase_retry just calls the callback immediately and returns ok=True
    mock_concurrency.rebase_retry.side_effect = lambda repo_root, fn: types.SimpleNamespace(ok=True)
    return mock_concurrency


# ---------------------------------------------------------------------------
# Tests: manifest contains dedup-create-skipped event
# ---------------------------------------------------------------------------


def test_manifest_contains_dedup_event_on_hit(applier, tmp_path):
    """apply() manifest includes a dedup-create-skipped event when JQL hit occurs."""
    existing_issue = {"key": "DIG-77", "fields": {}}
    mock_client = _make_mock_client(search_return=[existing_issue])

    # Stub accepts kwargs because applier.apply() constructs the client with
    # env-derived (jira_url, user, api_token) credentials.
    fake_acli = types.SimpleNamespace(AcliClient=lambda **_: mock_client)
    fake_concurrency = _make_mock_concurrency()
    with (
        patch.object(applier, "_load_acli", return_value=fake_acli),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        manifest_path = applier.apply(
            [_make_create_mutation("tick-ev1")],
            pass_id="test-pass-ev1",
            repo_root=tmp_path,
        )

    manifest = json.loads(manifest_path.read_text())
    events = manifest.get("events", [])
    assert len(events) == 1, f"Expected 1 event, got {len(events)}: {events!r}"
    ev = events[0]
    assert ev["event"] == "dedup-create-skipped"
    assert ev["local_id"] == "tick-ev1"
    assert ev["jira_key"] == "DIG-77"


def test_manifest_events_empty_on_no_hits(applier, tmp_path):
    """apply() manifest has an empty events list when no JQL hits occur."""
    mock_client = _make_mock_client(search_return=[])
    # Stub accepts kwargs because applier.apply() constructs the client with
    # env-derived (jira_url, user, api_token) credentials.
    fake_acli = types.SimpleNamespace(AcliClient=lambda **_: mock_client)
    fake_concurrency = _make_mock_concurrency()

    with (
        patch.object(applier, "_load_acli", return_value=fake_acli),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        manifest_path = applier.apply(
            [_make_create_mutation("tick-noev1")],
            pass_id="test-pass-noev1",
            repo_root=tmp_path,
        )

    manifest = json.loads(manifest_path.read_text())
    assert manifest.get("events") == [], "No events expected when JQL returns no hits"


def test_manifest_events_populated_for_each_hit(applier, tmp_path):
    """Multiple JQL hits produce one event per hit in the manifest events list."""
    existing_issue = {"key": "DIG-100", "fields": {}}
    mock_client = _make_mock_client(search_return=[existing_issue])
    # Stub accepts kwargs because applier.apply() constructs the client with
    # env-derived (jira_url, user, api_token) credentials.
    fake_acli = types.SimpleNamespace(AcliClient=lambda **_: mock_client)
    fake_concurrency = _make_mock_concurrency()

    mutations = [
        _make_create_mutation("tick-multi1"),
        _make_create_mutation("tick-multi2"),
    ]
    with (
        patch.object(applier, "_load_acli", return_value=fake_acli),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        manifest_path = applier.apply(
            mutations,
            pass_id="test-pass-multi",
            repo_root=tmp_path,
        )

    manifest = json.loads(manifest_path.read_text())
    events = manifest.get("events", [])
    assert len(events) == 2, f"Expected 2 events for 2 hits, got {events!r}"
    local_ids = {e["local_id"] for e in events}
    assert local_ids == {"tick-multi1", "tick-multi2"}


# ---------------------------------------------------------------------------
# Tests: atomic write (temp-file + rename)
# ---------------------------------------------------------------------------


def test_mapping_write_is_atomic_via_temp_rename(applier, tmp_path):
    """_write_mapping_atomic uses a temp file + os.replace (never writes directly).

    Strategy: patch os.replace to capture what paths are involved, and verify
    the source path is a sibling temp file (not mapping.json itself).
    """
    mapping_path = tmp_path / "bridge_state" / "mapping.json"

    rename_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def capturing_replace(src, dst):
        rename_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    with patch("os.replace", side_effect=capturing_replace):
        applier._write_mapping_atomic(mapping_path, "tick-atomic1", "DIG-555")

    assert len(rename_calls) == 1, f"Expected exactly one os.replace call, got {rename_calls!r}"
    src_path, dst_path = rename_calls[0]
    # Destination must be the canonical mapping.json path
    assert dst_path == str(mapping_path), (
        f"os.replace destination should be mapping.json, got {dst_path!r}"
    )
    # Source must be a sibling temp file (not mapping.json itself)
    assert src_path != dst_path, "Source and destination must differ (temp-rename pattern)"
    assert Path(src_path).parent == mapping_path.parent, (
        "Temp file should be in the same directory as mapping.json"
    )
    # The final mapping.json should contain the correct data
    data = json.loads(mapping_path.read_text())
    assert data.get("tick-atomic1") == "DIG-555"


def test_no_partial_mapping_visible_during_write(applier, tmp_path):
    """mapping.json is never in a truncated state: the file appears complete atomically.

    We verify this structurally: os.replace is atomic on POSIX. The test confirms
    the final file is valid JSON after _write_mapping_atomic completes.
    """
    mapping_path = tmp_path / "bridge_state" / "mapping.json"
    applier._write_mapping_atomic(mapping_path, "tick-partial1", "DIG-200")

    # File must exist and be fully parseable
    assert mapping_path.exists()
    data = json.loads(mapping_path.read_text())
    assert isinstance(data, dict)
    assert data["tick-partial1"] == "DIG-200"


def test_load_mapping_returns_empty_dict_for_non_dict_json(applier, tmp_path):
    """F10 regression: _load_mapping must return {} when JSON parses to a non-dict.

    Before F10, a corrupt write that produced a list / string / int parsed
    cleanly via json.loads but downstream ``data[jira_key] = ...`` raised
    TypeError. The fix guards with isinstance(data, dict) and returns {} so
    subsequent writes overwrite the corrupt file with a clean dict.
    """
    mapping_path = tmp_path / "bridge_state" / "mapping.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a JSON list (valid JSON, wrong shape)
    mapping_path.write_text(json.dumps([1, 2, 3]))

    loaded = applier._load_mapping(mapping_path)
    assert loaded == {}, f"_load_mapping must return {{}} for non-dict JSON; got {loaded!r}"

    # And a subsequent write must succeed cleanly, replacing the corrupt list
    applier._write_mapping_atomic(mapping_path, "tick-recover", "DIG-RECOVER")
    final = json.loads(mapping_path.read_text())
    assert isinstance(final, dict)
    assert final == {"tick-recover": "DIG-RECOVER"}
