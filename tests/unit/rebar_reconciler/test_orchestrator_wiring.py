"""Tests for orchestrator wiring: outbound/inbound differs, binding store,
sync logger, and reconcile-check mode integration into reconcile_once.

Follows the importlib loader convention established in conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(dotted_key: str, filename: str):
    """Load a reconciler sibling module under a stable sys.modules key."""
    if dotted_key in sys.modules:
        return sys.modules[dotted_key]
    path = RECONCILER_DIR / filename
    spec = importlib.util.spec_from_file_location(dotted_key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def reconcile_mod():
    return _load("reconcile_wiring_test", "reconcile.py")


@pytest.fixture(scope="module")
def mutation_mod():
    return _load("reconcile_wiring_mutation", "mutation.py")


@pytest.fixture(scope="module")
def mode_mod():
    return _load("reconcile_wiring_mode", "mode.py")


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("reconcile_wiring_binding_store", "binding_store.py")


@pytest.fixture(scope="module")
def sync_logger_mod():
    return _load("reconcile_wiring_sync_logger", "sync_logger.py")


@pytest.fixture(scope="module")
def outbound_differ_mod():
    return _load("reconcile_wiring_outbound_differ", "outbound_differ.py")


@pytest.fixture(scope="module")
def reconcile_check_mod():
    return _load("reconcile_wiring_reconcile_check", "reconcile_check.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_snapshot(tmp_path: Path, repo_root: Path, jira_snapshot: dict):
    """Write a Jira snapshot file so the fetcher stub can return its path."""
    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snapshots_dir / "test.json"
    snap_path.write_text(json.dumps(jira_snapshot))
    return snap_path


def _setup_repo_root(tmp_path: Path) -> Path:
    """Create a minimal repo_root with the directories reconcile_once expects."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    tracker = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    tracker.mkdir()
    bridge = tracker / ".bridge_state"
    bridge.mkdir(parents=True)
    bs_dir = repo_root / "bridge_state"
    bs_dir.mkdir()
    return repo_root


def _stub_modules(
    repo_root,
    jira_snapshot,
    legacy_mutations=None,
    local_tickets=None,
):
    """Return a dict of sys.modules stubs for reconcile_once dependencies.

    The stubs replace fetcher, differ, applier, health, invariants, and the
    new modules (binding_store, outbound_differ, inbound_differ, sync_logger).
    """
    snap_path = _make_fake_snapshot(None, repo_root, jira_snapshot)

    fetcher = MagicMock()
    fetcher.fetch_snapshot.return_value = snap_path

    differ = MagicMock()
    differ.compute_mutations.return_value = list(legacy_mutations or [])

    applier = MagicMock()
    applier.apply.return_value = repo_root / "bridge_state" / "manifest.json"
    # Give applier a search_issues method for binding recovery
    applier.search_issues = MagicMock(return_value=[])

    health_mod = MagicMock()
    health_mod.count_open_by_type.return_value = {}

    invariants_mod = MagicMock()
    invariants_mod.check_at_most_one_local_id.return_value = []
    invariants_mod.check_dual_identity_complete.return_value = (set(), [])

    # Binding store: real module with a mock store instance
    binding_store_stub = MagicMock()
    binding_store_stub.save = MagicMock()
    binding_store_stub.recover_pending_bindings = MagicMock()
    binding_store_stub.get_jira_key = MagicMock(return_value=None)
    binding_store_stub.is_bound = MagicMock(return_value=False)
    binding_store_stub.get_local_id = MagicMock(return_value=None)
    binding_store_stub.bind_pending = MagicMock()
    binding_store_stub.bind_confirm = MagicMock()

    binding_store_mod = MagicMock()
    binding_store_mod.load_binding_store.return_value = binding_store_stub

    # Outbound differ: returns OutboundMutation objects
    outbound_differ = MagicMock()
    outbound_differ.compute_outbound_mutations.return_value = []

    # Inbound differ
    inbound_differ = MagicMock()
    inbound_differ.compute_inbound_mutations.return_value = ([], 0)

    # Sync logger: a real-ish mock that tracks calls
    sync_logger_instance = MagicMock()
    sync_logger_cls = MagicMock(return_value=sync_logger_instance)
    sync_logger_module = MagicMock()
    sync_logger_module.SyncLogger = sync_logger_cls

    return {
        "reconcile_fetcher": fetcher,
        "reconcile_differ": differ,
        "reconcile_applier": applier,
        "reconcile_health": health_mod,
        "reconcile_invariants": invariants_mod,
        "reconcile_binding_store": binding_store_mod,
        "reconcile_outbound_differ": outbound_differ,
        "reconcile_inbound_differ": inbound_differ,
        "reconcile_sync_logger": sync_logger_module,
        # Keep the real mutation module for typed Mutation construction
        "reconcile_mutation": _load("reconcile_wiring_mutation", "mutation.py"),
        # Expose instances for assertions
        "_binding_store": binding_store_stub,
        "_sync_logger": sync_logger_instance,
        "_sync_logger_cls": sync_logger_cls,
    }


def _patch_and_run(
    reconcile_mod, stubs, repo_root, pass_id="test-pass", target_mode=None
):
    """Patch reconcile_mod._load to return stubs, then call reconcile_once."""
    original_load = reconcile_mod._load

    def fake_load(name, relpath):
        if name in stubs:
            return stubs[name]
        return original_load(name, relpath)

    # Also patch _read_local_tickets to return empty (no real CLI)
    with patch.object(reconcile_mod, "_load", side_effect=fake_load):
        with patch.object(
            reconcile_mod,
            "_read_local_tickets",
            return_value=[],
        ):
            return reconcile_mod.reconcile_once(
                pass_id,
                repo_root=repo_root,
                target_mode=target_mode,
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutboundCreate:
    """test_outbound_create_writes_binding_and_calls_jira"""

    def test_outbound_create_generates_typed_mutation(
        self,
        tmp_path,
        reconcile_mod,
        mutation_mod,
        outbound_differ_mod,
    ):
        """When outbound_differ emits a create, reconcile_once converts it to a
        typed outbound-create Mutation and includes it in the apply() call."""
        repo_root = _setup_repo_root(tmp_path)
        jira_snapshot = {}
        stubs = _stub_modules(repo_root, jira_snapshot)

        # Set up outbound differ to return one create mutation
        om = outbound_differ_mod.OutboundMutation(
            local_id="abc-1234",
            jira_key=None,
            action="create",
            fields={"summary": "Test ticket", "issuetype": "Task"},
            comments=[],
            labels=[{"action": "add", "label": "team-a"}],
        )
        stubs["reconcile_outbound_differ"].compute_outbound_mutations.return_value = [
            om
        ]

        result = _patch_and_run(reconcile_mod, stubs, repo_root)

        # The applier.apply should have been called with mutations that include
        # the outbound create
        apply_call = stubs["reconcile_applier"].apply
        assert apply_call.called
        mutations_passed = apply_call.call_args[0][0]
        outbound_creates = [
            m
            for m in mutations_passed
            if hasattr(m, "direction")
            and str(getattr(m.direction, "value", m.direction)) == "outbound"
            and str(getattr(m.action, "value", m.action)) == "create"
        ]
        assert len(outbound_creates) >= 1
        assert outbound_creates[0].target == "abc-1234"


class TestOutboundUpdate:
    """test_outbound_update_routes_fields_to_correct_methods"""

    def test_outbound_update_generates_typed_mutation_with_changed_fields(
        self,
        tmp_path,
        reconcile_mod,
        mutation_mod,
        outbound_differ_mod,
    ):
        """When outbound_differ emits an update, the changed_fields are
        carried in the typed Mutation payload."""
        repo_root = _setup_repo_root(tmp_path)
        jira_snapshot = {}
        stubs = _stub_modules(repo_root, jira_snapshot)

        om = outbound_differ_mod.OutboundMutation(
            local_id="abc-1234",
            jira_key="DIG-100",
            action="update",
            fields={"summary": "Updated title", "priority": "High"},
            comments=[],
            labels=[],
        )
        stubs["reconcile_outbound_differ"].compute_outbound_mutations.return_value = [
            om
        ]

        result = _patch_and_run(reconcile_mod, stubs, repo_root)

        apply_call = stubs["reconcile_applier"].apply
        mutations_passed = apply_call.call_args[0][0]
        outbound_updates = [
            m
            for m in mutations_passed
            if hasattr(m, "direction")
            and str(getattr(m.direction, "value", m.direction)) == "outbound"
            and str(getattr(m.action, "value", m.action)) == "update"
        ]
        assert len(outbound_updates) >= 1
        mut = outbound_updates[0]
        assert mut.target == "DIG-100"
        assert mut.payload["changed_fields"]["summary"] == "Updated title"
        assert mut.payload["changed_fields"]["priority"] == "High"


class TestInboundUpdate:
    """test_inbound_update_writes_local_event"""

    def test_inbound_differ_mutations_included_in_apply(
        self,
        tmp_path,
        reconcile_mod,
        mutation_mod,
    ):
        """When inbound_differ emits updates, they are converted to typed
        inbound Mutations and included in the apply() call."""
        repo_root = _setup_repo_root(tmp_path)
        jira_snapshot = {}
        stubs = _stub_modules(repo_root, jira_snapshot)

        # Create a mock InboundMutation
        im = SimpleNamespace(
            jira_key="DIG-200",
            local_id="def-5678",
            action="update",
            fields={"title": "Updated from Jira", "status": "in_progress"},
            labels=[],
        )
        stubs["reconcile_inbound_differ"].compute_inbound_mutations.return_value = (
            [im],
            0,
        )

        result = _patch_and_run(reconcile_mod, stubs, repo_root)

        apply_call = stubs["reconcile_applier"].apply
        mutations_passed = apply_call.call_args[0][0]
        inbound_updates = [
            m
            for m in mutations_passed
            if hasattr(m, "direction")
            and str(getattr(m.direction, "value", m.direction)) == "inbound"
            and str(getattr(m.action, "value", m.action)) == "update"
            and hasattr(m, "provenance")
            and m.provenance.get("source") == "inbound_differ"
        ]
        assert len(inbound_updates) >= 1
        mut = inbound_updates[0]
        assert mut.target == "DIG-200"
        assert mut.payload["local_id"] == "def-5678"


class TestReconcileCheckMode:
    """test_reconcile_check_mode_produces_report"""

    def test_reconcile_check_returns_json_report(
        self,
        tmp_path,
        reconcile_check_mod,
    ):
        """reconcile_check() returns a structured report with expected keys."""
        local_tickets = [
            {"id": "abc-1", "title": "Test", "status": "open"},
        ]
        jira_snapshot = {
            "DIG-1": {"summary": "Test", "status": "To Do"},
        }

        # Minimal binding store stub.
        # all_bindings() returns dict[local_id, entry] per the BindingStore
        # protocol — reconcile_check.reconcile_check iterates it via .items()
        # (bug 0776 — list shape would crash with AttributeError).
        class FakeBindings:
            def all_bindings(self):
                return {"abc-1": {"jira_key": "DIG-1", "state": "confirmed"}}

        report = reconcile_check_mod.reconcile_check(
            local_tickets,
            jira_snapshot,
            FakeBindings(),
        )
        assert "total_bindings" in report
        assert "checked" in report
        assert "in_sync" in report
        assert "discrepancies" in report
        assert report["total_bindings"] == 1
        assert report["checked"] == 1


class TestCapCombined:
    """test_cap_applies_to_combined_mutations"""

    def test_combined_outbound_and_legacy_mutations_passed_to_apply(
        self,
        tmp_path,
        reconcile_mod,
        mutation_mod,
        outbound_differ_mod,
    ):
        """Both legacy inbound mutations and outbound mutations flow through
        the same applier.apply() call, so mode-cap enforcement applies to the
        combined set."""
        repo_root = _setup_repo_root(tmp_path)
        jira_snapshot = {}

        # Create some legacy mutations (from the snapshot differ)
        D = mutation_mod.MutationDirection
        A = mutation_mod.MutationAction
        legacy = [
            mutation_mod.Mutation(
                direction=D.inbound,
                action=A.create,
                target="DIG-999",
                payload={"key": "DIG-999"},
                provenance={"source": "differ"},
            ),
        ]
        stubs = _stub_modules(repo_root, jira_snapshot, legacy_mutations=legacy)

        # Add an outbound mutation too
        om = outbound_differ_mod.OutboundMutation(
            local_id="xyz-001",
            jira_key=None,
            action="create",
            fields={"summary": "New", "issuetype": "Task"},
            comments=[],
            labels=[],
        )
        stubs["reconcile_outbound_differ"].compute_outbound_mutations.return_value = [
            om
        ]

        result = _patch_and_run(reconcile_mod, stubs, repo_root)

        apply_call = stubs["reconcile_applier"].apply
        mutations_passed = apply_call.call_args[0][0]
        # Should have both the legacy inbound + the outbound create
        assert len(mutations_passed) >= 2
        assert result["mutation_count"] >= 2


class TestSyncLogger:
    """test_sync_logger_created_and_closed"""

    def test_sync_logger_lifecycle(
        self,
        tmp_path,
        reconcile_mod,
    ):
        """SyncLogger is created at pass start with sync_pass_start, and
        closed at pass end with sync_pass_end."""
        repo_root = _setup_repo_root(tmp_path)
        stubs = _stub_modules(repo_root, jira_snapshot={})

        result = _patch_and_run(reconcile_mod, stubs, repo_root, pass_id="p1")

        sync_logger = stubs["_sync_logger"]
        # Check that log was called with sync_pass_start and sync_pass_end
        log_events = [c.args[0] for c in sync_logger.log.call_args_list]
        assert "sync_pass_start" in log_events
        assert "sync_pass_end" in log_events
        # Verify close was called
        assert sync_logger.close.called


class TestBindingStoreSaved:
    """test_binding_store_saved_at_pass_end"""

    def test_binding_store_save_called(
        self,
        tmp_path,
        reconcile_mod,
    ):
        """The binding store's save() method is called after apply completes."""
        repo_root = _setup_repo_root(tmp_path)
        stubs = _stub_modules(repo_root, jira_snapshot={})

        result = _patch_and_run(reconcile_mod, stubs, repo_root)

        binding_store = stubs["_binding_store"]
        assert binding_store.save.called
