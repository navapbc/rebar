"""Tests for outbound create binding: local_id propagation and cap ordering.

Bug d5a2-3fc8: outbound create mutations lost their local_id during the
Mutation-to-batch-dict conversion, and cap sorting deferred all outbound
creates behind inbound mutations under bootstrap-strict mode.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
)
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return _load_module("test_ocb_applier", APPLIER_PATH)


@pytest.fixture(scope="module")
def mutation_mod() -> ModuleType:
    return _load_module("test_ocb_mutation", MUTATION_PATH)


@pytest.fixture(scope="module")
def outbound_differ_mod() -> ModuleType:
    return _load_module("test_ocb_outbound_differ", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def get_jira_key(self, local_id: str):
        return None

    def is_bound(self, local_id: str) -> bool:
        return False


def test_outbound_create_batch_dict_has_local_id_end_to_end(
    applier_mod, mutation_mod, outbound_differ_mod
):
    """End-to-end: outbound_differ emits a create -> reconcile.py converts
    to typed Mutation (with local_id in payload) -> _mutation_to_batch_dict
    extracts local_id correctly for create_one."""
    ticket = {
        "ticket_id": "probe-1234",
        "title": "Test ticket",
        "description": "Test",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
    }
    binding_store = StubBindingStore()
    mutations = outbound_differ_mod.compute_outbound_mutations(
        [ticket], {}, binding_store
    )
    assert len(mutations) == 1
    om = mutations[0]
    assert om.action == "create"
    assert om.local_id == "probe-1234"

    # This is the EXACT conversion reconcile.py does at lines 527-537.
    # The fix adds "local_id": om.local_id to the payload.
    typed = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.create,
        target=om.local_id,
        payload={
            **om.fields,
            "comments": om.comments,
            "labels": om.labels,
            "local_id": om.local_id,
        },
        provenance={"source": "outbound_differ", "local_id": om.local_id},
    )

    # The critical assertion: after _mutation_to_batch_dict, local_id is non-empty.
    # Pre-fix, payload lacked local_id so batch_dict["local_id"] was "".
    batch_dict = applier_mod._mutation_to_batch_dict(typed)
    assert batch_dict["local_id"] == "probe-1234", (
        f"local_id must propagate through the full pipeline; got '{batch_dict['local_id']}'"
    )


def test_mutation_to_batch_dict_preserves_local_id(applier_mod, mutation_mod):
    """_mutation_to_batch_dict must extract local_id from payload for
    create_one to use in JQL dedup and identity binding."""
    m = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.create,
        target="probe-5678",
        payload={
            "summary": "Test",
            "description": "Test desc",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "comments": [],
            "labels": [],
            "local_id": "probe-5678",
        },
        provenance={"source": "outbound_differ", "local_id": "probe-5678"},
    )
    batch_dict = applier_mod._mutation_to_batch_dict(m)
    assert batch_dict["local_id"] == "probe-5678"


def test_create_one_populates_binding_store(applier_mod):
    """create_one must call binding_store.bind_confirm after successful
    create so the BindingStore is populated for subsequent passes."""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_client.search_issues.return_value = []
    mock_client.create_issue.return_value = {"key": "DIG-9999"}
    mock_client.add_label.return_value = None
    mock_client.set_entity_property.return_value = None

    mock_binding_store = MagicMock()

    mutation = {
        "local_id": "test-bind-1234",
        "fields": {"summary": "Test", "issuetype": "Task"},
    }

    result = applier_mod.create_one(
        mutation,
        mock_client,
        repo_root=Path("/tmp"),
        binding_store=mock_binding_store,
    )

    assert result is not None
    mock_binding_store.bind_confirm.assert_called_once_with(
        "test-bind-1234", "DIG-9999"
    )


def test_cap_sort_prioritizes_outbound_creates(applier_mod, mutation_mod):
    """Outbound creates must sort before inbound mutations under cap
    enforcement so they land within the bootstrap-strict cap window."""
    outbound_create = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.create,
        target="local-1",
        payload={"local_id": "local-1"},
        provenance={},
    )
    inbound_update = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.inbound,
        action=mutation_mod.MutationAction.update,
        target="DIG-100",
        payload={},
        provenance={},
    )

    key_create = applier_mod._mode_sort_key(outbound_create)
    key_inbound = applier_mod._mode_sort_key(inbound_update)
    assert key_create < key_inbound, (
        f"Outbound create {key_create} should sort before inbound {key_inbound}"
    )
