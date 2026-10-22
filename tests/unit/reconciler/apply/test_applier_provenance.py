"""Tests for applier.py: field_provenance persistence after set-valued field conflict resolution.

RED task d633: verifies that when apply() or update_one() processes an update mutation
containing a set-valued field (e.g., "labels"), the provenance_record is persisted to
.rebar/bridge/mapping.json under mapping[jira_key]["field_provenance"][field_name].

All tests use tmp_path for isolation.
"""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rebar.reconciler import scratch_retention, ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


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


# applier.snapshot_head always returns this SHA, so no drift is detected.
_STABLE_HEAD = "abc1234def567890" * 2


def _make_mock_acli(update_return=None):
    """Return a (fake acli module, mock client) pair."""
    mock_client = MagicMock()
    mock_client.update_issue.return_value = (
        update_return if update_return is not None else {"key": "DIG-123"}
    )
    mock_client.search_issues.return_value = []
    # AcliClient stub must accept (jira_url, user, api_token) kwargs because
    # applier.apply() now constructs the client with env-derived credentials.
    fake_acli = mock_client  # S4: _load_acli returns the transport directly
    return fake_acli, mock_client


def _update(jira_key: str, fields: dict) -> Mutation:
    """A typed outbound update Mutation changing ``fields`` on ``jira_key``."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        jira_key,
        OutboundUpdatePayload(changed_fields=fields),
        {},
    )


def _make_update_mutation(jira_key: str = "DIG-123", labels=None, watchers=None) -> Mutation:
    """Return an update mutation with set-valued fields."""
    fields: dict = {}
    if labels is not None:
        fields["labels"] = labels
    if watchers is not None:
        fields["watchers"] = watchers
    return _update(jira_key, fields)


# ---------------------------------------------------------------------------
# Tests: provenance_record persisted to mapping.json after apply()
# ---------------------------------------------------------------------------


def test_provenance_persisted_for_labels_field(applier, tmp_path):
    """After apply() processes an update with 'labels', mapping.json contains
    field_provenance.labels.
    """
    mutation = _make_update_mutation("DIG-123", labels=["bug", "backend"])

    fake_acli, _ = _make_mock_acli()

    with (
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=fake_acli
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "snapshot_head", return_value=_STABLE_HEAD
        ),
    ):
        applier.apply(
            [mutation], pass_id="test-prov-01", repo_root=tmp_path, ticket_plans=_plans([mutation])
        )

    mapping_path = scratch_retention.mapping_path(tmp_path)
    assert mapping_path.exists(), "mapping.json must be created after update with set-valued field"

    data = json.loads(mapping_path.read_text())
    assert "DIG-123" in data, f"mapping.json must have entry for DIG-123, got: {data!r}"
    assert "field_provenance" in data["DIG-123"], (
        f"mapping['DIG-123'] must have 'field_provenance' key, got: {data['DIG-123']!r}"
    )
    provenance = data["DIG-123"]["field_provenance"]
    assert "labels" in provenance, f"field_provenance must contain 'labels', got: {provenance!r}"
    assert isinstance(provenance["labels"], list), (
        f"provenance_record for 'labels' must be a list, got: {type(provenance['labels'])}"
    )


def test_provenance_labels_contains_field_values(applier, tmp_path):
    """The persisted provenance_record for 'labels' includes the field values."""
    mutation = _make_update_mutation("DIG-456", labels=["frontend", "ux"])

    fake_acli, _ = _make_mock_acli(update_return={"key": "DIG-456"})

    with (
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=fake_acli
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "snapshot_head", return_value=_STABLE_HEAD
        ),
    ):
        applier.apply(
            [mutation], pass_id="test-prov-02", repo_root=tmp_path, ticket_plans=_plans([mutation])
        )

    mapping_path = scratch_retention.mapping_path(tmp_path)
    data = json.loads(mapping_path.read_text())
    labels_provenance = data["DIG-456"]["field_provenance"]["labels"]
    for label in ["frontend", "ux"]:
        assert label in labels_provenance, (
            f"Expected label {label!r} in provenance {labels_provenance!r}"
        )


def test_provenance_written_atomically(applier, tmp_path):
    """mapping.json provenance write uses temp-file + os.replace pattern."""
    import os
    from unittest.mock import patch

    mutation = _make_update_mutation("DIG-789", labels=["alpha"])
    fake_acli, _ = _make_mock_acli()

    rename_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def capturing_replace(src, dst):
        rename_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    with (
        patch.object(applier, "_load_acli", return_value=fake_acli),
        patch.object(applier, "snapshot_head", return_value=_STABLE_HEAD),
        patch("os.replace", side_effect=capturing_replace),
    ):
        applier.apply(
            [mutation],
            pass_id="test-prov-atomic",
            repo_root=tmp_path,
            ticket_plans=_plans([mutation]),
        )

    mapping_path = scratch_retention.mapping_path(tmp_path)
    # At least one os.replace call should target the mapping.json path
    mapping_renames = [(src, dst) for src, dst in rename_calls if dst == str(mapping_path)]
    assert len(mapping_renames) >= 1, (
        f"Expected at least one atomic rename to mapping.json, got: {rename_calls!r}"
    )
    # Source must be a sibling temp file
    for src, dst in mapping_renames:
        assert src != dst, "Source and destination must differ (temp-rename pattern)"
        assert Path(src).parent == mapping_path.parent, (
            "Temp file should be in same directory as mapping.json"
        )


def test_provenance_preserves_existing_mapping_entries(applier, tmp_path):
    """Provenance write does not clobber existing mapping.json entries."""
    mapping_path = scratch_retention.mapping_path(tmp_path)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-populate with an existing local_id -> jira_key mapping
    mapping_path.write_text(json.dumps({"old-local-id": "DIG-1"}))

    mutation = _make_update_mutation("DIG-999", labels=["ops"])
    fake_acli, _ = _make_mock_acli()

    with (
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=fake_acli
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "snapshot_head", return_value=_STABLE_HEAD
        ),
    ):
        applier.apply(
            [mutation],
            pass_id="test-prov-preserve",
            repo_root=tmp_path,
            ticket_plans=_plans([mutation]),
        )

    data = json.loads(mapping_path.read_text())
    assert data.get("old-local-id") == "DIG-1", "Pre-existing mapping entry must be preserved"
    assert "DIG-999" in data, "New provenance entry for DIG-999 must be present"


def test_concurrent_provenance_writes_merge_distinct_issue_keys(tmp_path):
    """Independent provenance writes must merge rather than snapshot-clobber."""
    pass_io = __import__("rebar.reconciler.pass_io", fromlist=["_persist_field_provenance"])
    mapping_path = scratch_retention.mapping_path(tmp_path)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps({"old-local-id": "DIG-1"}), encoding="utf-8")
    errors: list[BaseException] = []

    def worker(jira_key: str, label: str) -> None:
        try:
            pass_io._persist_field_provenance(mapping_path, jira_key, "labels", [label])
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("DIG-LEFT", "left")),
        threading.Thread(target=worker, args=("DIG-RIGHT", "right")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    data = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert data["old-local-id"] == "DIG-1"
    assert data["DIG-LEFT"]["field_provenance"]["labels"] == ["left"]
    assert data["DIG-RIGHT"]["field_provenance"]["labels"] == ["right"]


def test_provenance_multiple_set_valued_fields(applier, tmp_path):
    """Both 'labels' and 'watchers' get their own provenance_record entry."""
    mutation = _make_update_mutation(
        "DIG-300", labels=["tag-a", "tag-b"], watchers=["alice", "bob"]
    )
    fake_acli, _ = _make_mock_acli()

    with (
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=fake_acli
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "snapshot_head", return_value=_STABLE_HEAD
        ),
    ):
        applier.apply(
            [mutation],
            pass_id="test-prov-multi-field",
            repo_root=tmp_path,
            ticket_plans=_plans([mutation]),
        )

    mapping_path = scratch_retention.mapping_path(tmp_path)
    data = json.loads(mapping_path.read_text())
    provenance = data["DIG-300"]["field_provenance"]
    assert "labels" in provenance, "Expected 'labels' in field_provenance"
    assert "watchers" in provenance, "Expected 'watchers' in field_provenance"
    assert isinstance(provenance["labels"], list)
    assert isinstance(provenance["watchers"], list)


def test_non_set_valued_fields_do_not_create_provenance(applier, tmp_path):
    """Update mutations with only non-set-valued fields do not create mapping.json provenance."""
    mutation = _update("DIG-400", {"status": "Done", "priority": "High"})  # both "state" class
    fake_acli, _ = _make_mock_acli()

    with (
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=fake_acli
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "snapshot_head", return_value=_STABLE_HEAD
        ),
    ):
        applier.apply(
            [mutation],
            pass_id="test-prov-no-set",
            repo_root=tmp_path,
            ticket_plans=_plans([mutation]),
        )

    mapping_path = scratch_retention.mapping_path(tmp_path)
    # mapping.json may or may not exist — if it does, DIG-400 should not have field_provenance
    if mapping_path.exists():
        data = json.loads(mapping_path.read_text())
        if "DIG-400" in data:
            assert "field_provenance" not in data["DIG-400"], (
                "Non-set-valued fields must not create field_provenance entries"
            )
