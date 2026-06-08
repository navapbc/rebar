"""RED tests for bidirectional parent/child hierarchy sync (ticket 8b25-ae7a-efc3-47f6).

Covers all seams:
  - outbound create with bound parent → payload carries parent key
  - outbound create unbound parent → no parent + debug skip
  - outbound reparent (bound ticket parent change) → set_parent called
  - inbound create with parent → local parent_id populated (bound) or empty (unbound)
  - inbound update parent change → EDIT with parent_id
  - reducer applies parent_id from EDIT (already works via process_edit)
  - fetcher enrichment failure degrades gracefully

All tests use mock clients; no live Jira calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_BASE = REPO_ROOT / "src" / "rebar" / "_engine"
_REC = _BASE / "dso_reconciler"

OUTBOUND_DIFFER_PATH = _REC / "outbound_differ.py"
INBOUND_DIFFER_PATH = _REC / "inbound_differ.py"
APPLIER_PATH = _REC / "applier.py"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ_parent_test", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load("inbound_differ_parent_test", INBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


class StubOutboundBindingStore:
    """Outbound: maps local_id → jira_key."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


class StubInboundBindingStore:
    """Inbound: maps jira_key → local_id."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


class StubClient:
    """Minimal mock client that records set_parent calls."""

    def __init__(self) -> None:
        self.set_parent_calls: list[tuple[str, str | None]] = []

    def set_parent(self, jira_key: str, parent_key: str | None) -> None:
        self.set_parent_calls.append((jira_key, parent_key))

    def get_comments(self, jira_key: str) -> list:
        return []


def _make_ticket(
    ticket_id: str = "abc-1234",
    title: str = "Fix the widget",
    description: str = "It is broken",
    status: str = "open",
    priority: int = 2,
    ticket_type: str = "bug",
    assignee: str = "alice",
    tags: list[str] | None = None,
    comments: list[dict] | None = None,
    parent_id: str | None = None,
) -> dict:
    t: dict[str, Any] = {
        "ticket_id": ticket_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "ticket_type": ticket_type,
        "assignee": assignee,
        "tags": tags or [],
        "comments": comments or [],
    }
    if parent_id is not None:
        t["parent_id"] = parent_id
    return t


# ===========================================================================
# OUTBOUND TESTS
# ===========================================================================


class TestOutboundParent:
    """Outbound parent/child sync tests."""

    def test_create_with_bound_parent_carries_parent_key(
        self, outbound_differ: ModuleType
    ) -> None:
        """Outbound CREATE with bound parent_id → fields carries parent key."""
        # parent local_id "parent-1" is bound to "DIG-10"
        store = StubOutboundBindingStore({"parent-1": "DIG-10"})
        ticket = _make_ticket(ticket_id="child-1", parent_id="parent-1")
        mutations = outbound_differ.compute_outbound_mutations(
            [ticket], jira_snapshot={}, binding_store=store
        )
        assert len(mutations) == 1
        mut = mutations[0]
        assert mut.action == "create"
        # The parent key must appear in the mutation fields
        assert mut.fields.get("parent") == "DIG-10", (
            f"Expected parent='DIG-10' in create fields, got: {mut.fields}"
        )

    def test_create_with_unbound_parent_skips_parent(
        self, outbound_differ: ModuleType, capsys
    ) -> None:
        """Outbound CREATE with unbound parent_id → no parent in payload; debug logged."""
        # "unknown-parent" is NOT in the binding store
        store = StubOutboundBindingStore({})
        ticket = _make_ticket(ticket_id="child-2", parent_id="unknown-parent")
        mutations = outbound_differ.compute_outbound_mutations(
            [ticket], jira_snapshot={}, binding_store=store
        )
        assert len(mutations) == 1
        mut = mutations[0]
        assert mut.action == "create"
        # Parent must NOT appear in fields when unbound (skip + log)
        assert "parent" not in mut.fields, (
            f"parent should be absent for unbound parent_id, got: {mut.fields}"
        )

    def test_create_without_parent_id_no_parent_field(
        self, outbound_differ: ModuleType
    ) -> None:
        """Outbound CREATE with no parent_id → no parent field emitted."""
        store = StubOutboundBindingStore({})
        ticket = _make_ticket(ticket_id="orphan-1")
        # No parent_id key at all
        mutations = outbound_differ.compute_outbound_mutations(
            [ticket], jira_snapshot={}, binding_store=store
        )
        assert len(mutations) == 1
        assert "parent" not in mutations[0].fields

    def test_reparent_bound_ticket_emits_parent_change(
        self, outbound_differ: ModuleType
    ) -> None:
        """Bound ticket with changed parent_id → update mutation carries parent field."""
        # child-3 is bound to DIG-30; parent-3 is bound to DIG-20
        store = StubOutboundBindingStore({"child-3": "DIG-30", "parent-3": "DIG-20"})
        ticket = _make_ticket(ticket_id="child-3", parent_id="parent-3")
        # Jira snapshot for DIG-30 has NO parent (None / absent)
        jira_snapshot = {
            "DIG-30": {
                "summary": "Fix the widget",
                "description": "It is broken",
                "issuetype": {"name": "Bug"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": {"displayName": "alice"},
                "labels": [],
                # parent absent → ticket had no parent on Jira side
            }
        }
        mutations = outbound_differ.compute_outbound_mutations(
            [ticket], jira_snapshot=jira_snapshot, binding_store=store
        )
        # An update mutation should be emitted because parent changed
        update_muts = [
            m for m in mutations if m.action == "update" and m.local_id == "child-3"
        ]
        assert len(update_muts) == 1, (
            f"Expected exactly one update mutation for child-3, got: {mutations}"
        )
        assert update_muts[0].fields.get("parent") == "DIG-20", (
            f"Expected parent='DIG-20' in update fields, got: {update_muts[0].fields}"
        )

    def test_no_reparent_when_parent_unchanged(
        self, outbound_differ: ModuleType
    ) -> None:
        """Bound ticket with same parent on both sides → no parent in update fields."""
        store = StubOutboundBindingStore({"child-4": "DIG-40", "parent-4": "DIG-41"})
        ticket = _make_ticket(ticket_id="child-4", parent_id="parent-4")
        jira_snapshot = {
            "DIG-40": {
                "summary": "Fix the widget",
                "description": "It is broken",
                "issuetype": {"name": "Bug"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": {"displayName": "alice"},
                "labels": [],
                # Jira already has the correct parent
                "parent": {"key": "DIG-41"},
            }
        }
        mutations = outbound_differ.compute_outbound_mutations(
            [ticket], jira_snapshot=jira_snapshot, binding_store=store
        )
        update_muts = [
            m for m in mutations if m.action == "update" and m.local_id == "child-4"
        ]
        # Either no mutation or mutation without parent in fields
        if update_muts:
            assert "parent" not in update_muts[0].fields, (
                f"parent should not be in fields when unchanged: {update_muts[0].fields}"
            )


# ===========================================================================
# INBOUND TESTS
# ===========================================================================


class TestInboundParent:
    """Inbound parent/child sync tests."""

    def test_inbound_create_populates_parent_id_when_bound(
        self, inbound_differ: ModuleType
    ) -> None:
        """Inbound diff for issue with Jira parent → parent_id in changed fields (bound)."""
        # DIG-50 is a child of DIG-49; DIG-49 is bound to local "parent-local-49"
        jira_snapshot = {
            "DIG-50": {
                "summary": "Child ticket",
                "description": "",
                "issuetype": {"name": "Story"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": None,
                "labels": [],
                "parent": {"key": "DIG-49"},
            }
        }
        store = StubInboundBindingStore(
            {"DIG-50": "jira-dig-50", "DIG-49": "jira-dig-49"}
        )
        local_tickets: dict[str, dict] = {
            "jira-dig-50": {
                "ticket_id": "jira-dig-50",
                "title": "Child ticket",
                "description": "",
                "ticket_type": "story",
                "priority": 2,
                "status": "open",
                "assignee": "",
                "tags": [],
                "parent_id": None,  # not yet set locally
            }
        }
        mutations, _ = inbound_differ.compute_inbound_mutations(
            jira_snapshot=jira_snapshot,
            binding_store=store,
            local_tickets_by_id=local_tickets,
        )
        assert len(mutations) == 1, f"Expected 1 inbound mutation, got: {mutations}"
        mut = mutations[0]
        assert "parent_id" in mut.fields, (
            f"Expected parent_id in inbound fields, got: {mut.fields}"
        )
        assert mut.fields["parent_id"] == "jira-dig-49", (
            f"Expected parent_id='jira-dig-49', got: {mut.fields.get('parent_id')}"
        )

    def test_inbound_skips_parent_when_unbound(
        self, inbound_differ: ModuleType
    ) -> None:
        """Inbound diff: parent key not in binding store → parent_id NOT in fields."""
        jira_snapshot = {
            "DIG-51": {
                "summary": "Child ticket",
                "description": "",
                "issuetype": {"name": "Story"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": None,
                "labels": [],
                "parent": {"key": "DIG-UNKNOWN"},  # not bound
            }
        }
        # DIG-51 bound, DIG-UNKNOWN not bound
        store = StubInboundBindingStore({"DIG-51": "jira-dig-51"})
        local_tickets: dict[str, dict] = {
            "jira-dig-51": {
                "ticket_id": "jira-dig-51",
                "title": "Child ticket",
                "description": "",
                "ticket_type": "story",
                "priority": 2,
                "status": "open",
                "assignee": "",
                "tags": [],
                "parent_id": None,
            }
        }
        mutations, _ = inbound_differ.compute_inbound_mutations(
            jira_snapshot=jira_snapshot,
            binding_store=store,
            local_tickets_by_id=local_tickets,
        )
        # When parent is unbound, parent_id should NOT appear in fields
        for mut in mutations:
            assert "parent_id" not in mut.fields, (
                f"parent_id should be absent when parent key is unbound: {mut.fields}"
            )

    def test_inbound_update_parent_change(self, inbound_differ: ModuleType) -> None:
        """Inbound diff: parent changed on Jira → parent_id emitted in update fields."""
        jira_snapshot = {
            "DIG-52": {
                "summary": "Changed parent",
                "description": "",
                "issuetype": {"name": "Task"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": None,
                "labels": [],
                "parent": {"key": "DIG-53"},  # new parent
            }
        }
        store = StubInboundBindingStore(
            {"DIG-52": "jira-dig-52", "DIG-53": "jira-dig-53"}
        )
        local_tickets: dict[str, dict] = {
            "jira-dig-52": {
                "ticket_id": "jira-dig-52",
                "title": "Changed parent",
                "description": "",
                "ticket_type": "task",
                "priority": 2,
                "status": "open",
                "assignee": "",
                "tags": [],
                "parent_id": "jira-dig-OLD",  # different from Jira
            }
        }
        mutations, _ = inbound_differ.compute_inbound_mutations(
            jira_snapshot=jira_snapshot,
            binding_store=store,
            local_tickets_by_id=local_tickets,
        )
        assert len(mutations) == 1
        assert mutations[0].fields.get("parent_id") == "jira-dig-53", (
            f"Expected updated parent_id, got: {mutations[0].fields}"
        )

    def test_inbound_no_diff_when_parent_matches(
        self, inbound_differ: ModuleType
    ) -> None:
        """Inbound diff: Jira parent matches local parent → no parent_id in fields."""
        jira_snapshot = {
            "DIG-54": {
                "summary": "Same parent",
                "description": "",
                "issuetype": {"name": "Task"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": None,
                "labels": [],
                "parent": {"key": "DIG-55"},
            }
        }
        store = StubInboundBindingStore(
            {"DIG-54": "jira-dig-54", "DIG-55": "jira-dig-55"}
        )
        local_tickets: dict[str, dict] = {
            "jira-dig-54": {
                "ticket_id": "jira-dig-54",
                "title": "Same parent",
                "description": "",
                "ticket_type": "task",
                "priority": 2,
                "status": "open",
                "assignee": "",
                "tags": [],
                "parent_id": "jira-dig-55",  # already matches
            }
        }
        mutations, _ = inbound_differ.compute_inbound_mutations(
            jira_snapshot=jira_snapshot,
            binding_store=store,
            local_tickets_by_id=local_tickets,
        )
        # Should produce no mutations (all fields match)
        for mut in mutations:
            assert "parent_id" not in mut.fields, (
                f"parent_id should not appear when it already matches: {mut.fields}"
            )


# ===========================================================================
# APPLIER INBOUND CREATE parent_id TEST
# ===========================================================================


class TestApplierInboundCreateParent:
    """Applier: _apply_inbound_create populates parent_id when payload carries it."""

    def _load_applier(self) -> ModuleType:
        key = "applier_parent_test"
        return _load(key, APPLIER_PATH)

    def test_inbound_create_sets_parent_id_from_payload(self, tmp_path: Path) -> None:
        """_apply_inbound_create: parent key in fields → CREATE event has parent_id."""
        import os

        applier = self._load_applier()
        mut_mod = importlib.util.spec_from_file_location(
            "mutation_parent_test", _REC / "mutation.py"
        )
        assert mut_mod is not None and mut_mod.loader is not None
        mm = importlib.util.module_from_spec(mut_mod)
        sys.modules.setdefault("mutation_parent_test", mm)
        mut_mod.loader.exec_module(mm)  # type: ignore[union-attr]

        tracker_dir = tmp_path / ".tickets-tracker"
        tracker_dir.mkdir()

        # Build a minimal inbound create mutation with a parent field
        mut = mm.Mutation(
            direction=mm.MutationDirection.inbound,
            action=mm.MutationAction.create,
            target="DIG-60",
            payload={
                "fields": {
                    "summary": "Child issue",
                    "issuetype": {"name": "Story"},
                    "description": "",
                    "parent": {"key": "DIG-59"},  # Jira parent
                },
                # binding_store provides local mapping: DIG-59 → jira-dig-59
                "_parent_local_id": "jira-dig-59",  # resolver must extract this
            },
            provenance={},
        )

        # We use TICKETS_TRACKER_DIR env var to isolate the tracker dir
        env_backup = os.environ.get("TICKETS_TRACKER_DIR")
        try:
            os.environ["TICKETS_TRACKER_DIR"] = str(tracker_dir)
            applier.apply(mut, client=None, repo_root=tmp_path)
        finally:
            if env_backup is None:
                os.environ.pop("TICKETS_TRACKER_DIR", None)
            else:
                os.environ["TICKETS_TRACKER_DIR"] = env_backup

        import json

        # Read the CREATE event that was written
        local_id = "jira-dig-60"
        ticket_dir = tracker_dir / local_id
        assert ticket_dir.exists(), f"Ticket dir not created: {ticket_dir}"
        events = list(ticket_dir.glob("*-CREATE.json"))
        assert events, f"No CREATE event found in {ticket_dir}"
        create_data = json.loads(events[0].read_text())
        # parent_id should be populated from the parent key resolution
        assert create_data["data"].get("parent_id") == "jira-dig-59", (
            f"Expected parent_id='jira-dig-59', got: {create_data['data']}"
        )


# ===========================================================================
# APPLIER INBOUND UPDATE parent_id TEST
# ===========================================================================


class TestApplierInboundUpdateParent:
    """Applier: _apply_inbound_update handles parent_id in fields dict."""

    def _load_applier(self) -> ModuleType:
        key = "applier_parent_update_test"
        return _load(key, APPLIER_PATH)

    def test_inbound_update_writes_parent_id_in_edit_event(
        self, tmp_path: Path
    ) -> None:
        """_apply_inbound_update: parent_id in fields → EDIT event carries parent_id."""
        import json
        import os

        applier = self._load_applier()
        mut_mod_spec = importlib.util.spec_from_file_location(
            "mutation_parent_upd_test", _REC / "mutation.py"
        )
        assert mut_mod_spec is not None and mut_mod_spec.loader is not None
        mm = importlib.util.module_from_spec(mut_mod_spec)
        sys.modules.setdefault("mutation_parent_upd_test", mm)
        mut_mod_spec.loader.exec_module(mm)  # type: ignore[union-attr]

        tracker_dir = tmp_path / ".tickets-tracker"
        ticket_dir = tracker_dir / "jira-dig-70"
        ticket_dir.mkdir(parents=True)
        # Write a minimal CREATE event so the ticket exists
        import time
        import uuid as _uuid

        ts = time.time_ns()
        uid = str(_uuid.uuid4())
        create_event = {
            "timestamp": ts,
            "uuid": uid,
            "event_type": "CREATE",
            "env_id": "test",
            "author": "test",
            "data": {
                "id": "jira-dig-70",
                "ticket_type": "task",
                "title": "Test",
                "description": "",
                "parent_id": "jira-dig-OLD",
                "tags": [],
            },
        }
        (ticket_dir / f"{ts}-{uid}-CREATE.json").write_text(json.dumps(create_event))

        mut = mm.Mutation(
            direction=mm.MutationDirection.inbound,
            action=mm.MutationAction.update,
            target="DIG-70",
            payload={
                "local_id": "jira-dig-70",
                "fields": {
                    "parent_id": "jira-dig-NEW",  # new parent
                },
            },
            provenance={},
        )

        env_backup = os.environ.get("TICKETS_TRACKER_DIR")
        try:
            os.environ["TICKETS_TRACKER_DIR"] = str(tracker_dir)
            applier.apply(mut, client=None, repo_root=tmp_path)
        finally:
            if env_backup is None:
                os.environ.pop("TICKETS_TRACKER_DIR", None)
            else:
                os.environ["TICKETS_TRACKER_DIR"] = env_backup

        # Check that an EDIT event was written with parent_id in fields
        edit_events = list(ticket_dir.glob("*-EDIT.json"))
        assert edit_events, f"No EDIT event found in {ticket_dir}"
        found_parent = False
        for ef in edit_events:
            ed = json.loads(ef.read_text())
            if "parent_id" in (ed.get("data", {}).get("fields", {})):
                found_parent = True
                assert ed["data"]["fields"]["parent_id"] == "jira-dig-NEW"
        assert found_parent, (
            f"parent_id not found in any EDIT event: {list(ticket_dir.glob('*.json'))}"
        )


# ===========================================================================
# APPLIER OUTBOUND UPDATE parent via set_parent TEST
# ===========================================================================


class TestApplierOutboundSetParent:
    """Applier: _apply_outbound_update routes parent → client.set_parent."""

    def _load_applier(self) -> ModuleType:
        key = "applier_outbound_parent_test"
        return _load(key, APPLIER_PATH)

    def test_outbound_update_calls_set_parent(self, tmp_path: Path) -> None:
        """_apply_outbound_update: parent field in changed_fields → client.set_parent called."""
        applier = self._load_applier()
        mut_mod_spec = importlib.util.spec_from_file_location(
            "mutation_ob_parent_test", _REC / "mutation.py"
        )
        assert mut_mod_spec is not None and mut_mod_spec.loader is not None
        mm = importlib.util.module_from_spec(mut_mod_spec)
        sys.modules.setdefault("mutation_ob_parent_test", mm)
        mut_mod_spec.loader.exec_module(mm)  # type: ignore[union-attr]

        client = StubClient()
        mut = mm.Mutation(
            direction=mm.MutationDirection.outbound,
            action=mm.MutationAction.update,
            target="DIG-80",
            payload={
                "changed_fields": {
                    "parent": "DIG-81",
                },
            },
            provenance={},
        )
        applier.apply(mut, client=client, repo_root=tmp_path)
        assert client.set_parent_calls == [("DIG-80", "DIG-81")], (
            f"Expected set_parent('DIG-80', 'DIG-81'), got: {client.set_parent_calls}"
        )


# ===========================================================================
# FETCHER DEGRADATION TEST
# ===========================================================================


class TestFetcherParentEnrichment:
    """Fetcher: parent enrichment failure degrades gracefully (no block)."""

    def test_snapshot_enriches_parent_field(self, tmp_path: Path) -> None:
        """fetch_snapshot: issues in snapshot get parent field from REST lookup."""
        import importlib.util as _iu
        import sys

        fetcher_path = _REC / "fetcher.py"
        key = "fetcher_parent_test"
        if key in sys.modules:
            del sys.modules[key]
        spec = _iu.spec_from_file_location(key, fetcher_path)
        assert spec is not None and spec.loader is not None
        fetcher_mod = _iu.module_from_spec(spec)
        sys.modules[key] = fetcher_mod
        spec.loader.exec_module(fetcher_mod)  # type: ignore[union-attr]

        class _StubClient:
            """Client that returns one issue with a parent field."""

            _call_count = 0

            def search_issues(self, jql, start_at=0, max_results=50):
                if self._call_count == 0:
                    self._call_count += 1
                    return [
                        {
                            "key": "DIG-90",
                            "fields": {
                                "summary": "Test",
                                "issuetype": {"name": "Story"},
                                "parent": {"key": "DIG-91"},
                            },
                        }
                    ]
                return []  # subsequent pages empty

        class _StubAcliMod:
            """Module stub so fetcher._load_acli() returns this."""

            @staticmethod
            def AcliClient(**kwargs):
                return _StubClient()

        # Patch _load_acli to return our stub
        original_load = fetcher_mod._load_acli
        fetcher_mod._load_acli = lambda: _StubAcliMod


        bridge_state = tmp_path / "bridge_state" / "snapshots"
        bridge_state.mkdir(parents=True)

        try:
            output_path = fetcher_mod.fetch_snapshot("test-pass", repo_root=tmp_path)
        finally:
            fetcher_mod._load_acli = original_load

        import json

        snapshot = json.loads(output_path.read_text())
        assert "DIG-90" in snapshot, (
            f"DIG-90 missing from snapshot: {list(snapshot.keys())}"
        )
        # After enrichment, the snapshot entry should have a parent field
        assert snapshot["DIG-90"].get("parent") == {"key": "DIG-91"}, (
            f"Expected parent field in snapshot['DIG-90'], got: {snapshot['DIG-90']}"
        )

    def test_snapshot_degrades_gracefully_on_parent_enrichment_failure(
        self, tmp_path: Path
    ) -> None:
        """fetch_snapshot: REST parent-read failure → warning only, snapshot still written."""
        import importlib.util as _iu
        import sys

        fetcher_path = _REC / "fetcher.py"
        key = "fetcher_parent_degrade_test"
        if key in sys.modules:
            del sys.modules[key]
        spec = _iu.spec_from_file_location(key, fetcher_path)
        assert spec is not None and spec.loader is not None
        fetcher_mod = _iu.module_from_spec(spec)
        sys.modules[key] = fetcher_mod
        spec.loader.exec_module(fetcher_mod)  # type: ignore[union-attr]

        class _FailingClient:
            """Client whose get_parents call raises."""

            _call_count = 0

            def search_issues(self, jql, start_at=0, max_results=50):
                if self._call_count == 0:
                    self._call_count += 1
                    return [{"key": "DIG-95", "fields": {"summary": "Test"}}]
                return []

            def get_parent_map(self, project, jql=None):
                raise RuntimeError("REST parent fetch failed")

        class _StubAcliModFail:
            @staticmethod
            def AcliClient(**kwargs):
                return _FailingClient()

        fetcher_mod._load_acli = lambda: _StubAcliModFail
        bridge_state = tmp_path / "bridge_state" / "snapshots"
        bridge_state.mkdir(parents=True)

        try:
            # Should NOT raise even though parent enrichment fails
            output_path = fetcher_mod.fetch_snapshot(
                "test-degrade-pass", repo_root=tmp_path
            )
        finally:
            pass

        import json

        snapshot = json.loads(output_path.read_text())
        # Snapshot was still written despite failure
        assert "DIG-95" in snapshot, f"Snapshot should still contain DIG-95: {snapshot}"
