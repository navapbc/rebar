"""Pending bindings make create recovery deterministic.

Persist a pending record before create and its Jira key before labeling.
Keyed recovery confirms without search. Keyless recovery searches for the
identity label and unbinds only after corroborated absence. A failed pending
persist prevents create."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rebar.reconciler.create_route import _coordinated_create_core

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def binding_store_mod():
    return importlib.import_module("rebar.reconciler.binding_store")


@pytest.fixture(scope="module")
def dispatch():
    return importlib.import_module("rebar.reconciler.dispatch_one")


def _new_store(binding_store_mod, tmp_path):
    return binding_store_mod.BindingStore(tmp_path)


# --------------------------------------------------------------------------- #
# Recovery determinism (binding_store.recover_pending_bindings)
# --------------------------------------------------------------------------- #


def test_keyed_pending_recovered_without_search(binding_store_mod, tmp_path):
    """A keyed-pending record (crash in the create->label window) is confirmed with
    the label retro-attached and NO Jira search — no duplicate."""
    store = _new_store(binding_store_mod, tmp_path)
    store.bind_pending("local-A")
    store.record_pending_key("local-A", "DIG-100")

    client = MagicMock()
    recovered = store.recover_pending_bindings(client)

    assert recovered == 1
    client.search_issues.assert_not_called()  # deterministic — no search
    client.add_label.assert_called_once_with("DIG-100", "rebar-id:local-A")
    client.set_entity_property.assert_called_once_with("DIG-100", "local_id", "local-A")
    assert store.get_jira_key("local-A") == "DIG-100"
    assert "local-A" not in store.pending_bindings()  # now confirmed


def test_keyless_pending_found_via_search(binding_store_mod, tmp_path):
    """A keyless-pending record falls back to the rebar-id label search and confirms."""
    store = _new_store(binding_store_mod, tmp_path)
    store.bind_pending("local-B")  # no key recorded

    client = MagicMock()
    client.search_issues.return_value = [{"key": "DIG-200"}]
    recovered = store.recover_pending_bindings(client)

    assert recovered == 1
    client.search_issues.assert_called()  # keyless -> search fallback
    assert store.get_jira_key("local-B") == "DIG-200"
    assert "local-B" not in store.pending_bindings()


def test_keyless_pending_miss_unbinds(binding_store_mod, tmp_path):
    """A missing keyless pending label unbinds only after the configured absence checks agree."""
    store = _new_store(binding_store_mod, tmp_path)
    store.bind_pending("local-C")

    client = MagicMock()
    client.search_issues.return_value = []

    # ONE miss must NOT unbind — that is the duplicate-issue defect itself.
    assert store.recover_pending_bindings(client) == 0
    assert "local-C" in store.pending_bindings()

    # Age the entry past the index-lag grace window; without this the repeated misses
    # prove nothing, which is the whole point of the fix.
    store._data["bindings"]["local-C"]["created_at"] = "2000-01-01T00:00:00Z"
    counts = [store.recover_pending_bindings(client) for _ in range(3)]

    # Unbind after the third corroborated miss. Assert one resolution across the
    # sequence without depending on which call completes it.
    assert sum(counts) == 1, f"the corroborated unbind never resolved: {counts}"
    assert store.get_jira_key("local-C") is None
    assert "local-C" not in store.pending_bindings()


def test_recovery_failure_goes_to_sink_and_stays_pending(binding_store_mod, tmp_path):
    """A retro-attach failure appends to failure_sink and leaves the entry pending
    (retried next pass) — loud but non-fatal."""
    store = _new_store(binding_store_mod, tmp_path)
    store.bind_pending("local-D")
    store.record_pending_key("local-D", "DIG-300")

    client = MagicMock()
    client.add_label.side_effect = RuntimeError("jira down")
    failures: list[dict] = []
    recovered = store.recover_pending_bindings(client, failure_sink=failures)

    assert recovered == 0  # not resolved
    assert len(failures) == 1
    assert failures[0]["local_id"] == "local-D"
    assert "local-D" in store.pending_bindings()  # stays pending for next pass


# --------------------------------------------------------------------------- #
# Write-ahead ordering + persist-failure (dispatch_one.create_one)
# --------------------------------------------------------------------------- #


def test_write_ahead_orders_pending_before_create_and_key_before_label(dispatch, tmp_path):
    """create_one persists bind_pending BEFORE create_issue, and record_pending_key
    BEFORE add_label."""
    order: list[str] = []
    client = MagicMock()
    client.search_issues.return_value = []
    client.create_issue.side_effect = lambda *a, **k: (order.append("create"), {"key": "DIG-1"})[1]
    client.add_label.side_effect = lambda *a, **k: order.append("add_label")

    store = MagicMock()
    # MagicMock would make the keyless grace gate truthy and skip create. Make its
    # response false to model a store without pending grace.
    store.is_keyless_pending_within_grace.return_value = False
    store.bind_pending.side_effect = lambda *a, **k: order.append("bind_pending")
    store.record_pending_key.side_effect = lambda *a, **k: order.append("record_key")
    store.save.side_effect = lambda *a, **k: order.append("save")

    mutation = {
        "local_id": "wa-1",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
    }
    dispatch.create_one(
        mutation,
        client,
        repo_root=tmp_path,
        binding_store=store,
        create_core=_coordinated_create_core,
    )

    # bind_pending + its save come before create; record_key + its save before label.
    assert order.index("bind_pending") < order.index("create")
    assert order.index("record_key") < order.index("add_label")
    assert order.index("create") < order.index("record_key")


def test_bind_pending_persist_failure_skips_create(dispatch, binding_store_mod, tmp_path):
    """A bind_pending persist (save) failure raises BindingPersistError and
    create_issue is NEVER called (item-scoped skip)."""
    client = MagicMock()
    client.search_issues.return_value = []

    store = MagicMock()
    # See the note above: without this the 21fc grace gate defers the create, so no
    # bind_pending persist is attempted and BindingPersistError never raises.
    store.is_keyless_pending_within_grace.return_value = False
    store.save.side_effect = OSError("disk full")  # the pre-create persist fails

    mutation = {
        "local_id": "wa-2",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
    }
    with pytest.raises(binding_store_mod.BindingPersistError):
        dispatch.create_one(
            mutation,
            client,
            repo_root=tmp_path,
            binding_store=store,
            create_core=_coordinated_create_core,
        )

    client.create_issue.assert_not_called()


# --------------------------------------------------------------------------- #
# S4 T3 (2863-c335): the coordinated create route preserves the write-ahead
# protocol AND its deterministic keyed recovery (AC2).
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def route_mod():
    return importlib.import_module("rebar.reconciler.create_route")


class _RecordingClient:
    """A client that records the ORDER of create/label/property calls."""

    def __init__(self):
        self.order: list[str] = []

    def create_issue(self, payload):
        self.order.append("create_issue")
        return {"key": "DIG-900", "id": "10900"}

    def add_label(self, key, label):
        self.order.append("add_label")

    def set_entity_property(self, key, name, value):
        self.order.append("set_entity_property")

    def search_issues(self, jql):
        self.order.append("search_issues")
        return []


def test_coordinated_path_preserves_write_ahead_order(route_mod, binding_store_mod, tmp_path):
    """The coordinated write-ahead route records the key on the STILL-pending entry and
    persists it BEFORE the label, then confirms LAST — the canonical order."""
    store = binding_store_mod.BindingStore(tmp_path)
    client = _RecordingClient()
    mutation = type(
        "M", (), {"payload": {"local_id": "wa-coord", "summary": "s"}, "target": "wa-coord"}
    )()

    outcome = route_mod.run_coordinated_outbound_create(
        mutation, client=client, binding_store=store
    )

    assert outcome.confirmed is True
    assert outcome.dependents_released is True
    assert outcome.known_key == "DIG-900"
    # Canonical order: create BEFORE label, property AFTER label, no search on the
    # happy path (the key is captured from the create response).
    assert client.order == ["create_issue", "add_label", "set_entity_property"]
    # The binding is confirmed forward + reverse.
    assert store.get_jira_key("wa-coord") == "DIG-900"
    assert store.get_local_id("DIG-900") == "wa-coord"


def test_coordinated_abort_leaves_keyed_pending_recovered_without_search(
    route_mod, binding_store_mod, tmp_path
):
    """A label-write abort leaves keyed pending for search-free recovery."""
    store = binding_store_mod.BindingStore(tmp_path)

    class _LabelFails:
        def create_issue(self, payload):
            return {"key": "DIG-901", "id": "10901"}

        def add_label(self, key, label):
            raise RuntimeError("field off screen")

        def set_entity_property(self, key, name, value):
            raise AssertionError("must not reach property after label failure")

        def search_issues(self, jql):  # pragma: no cover - must not be called
            raise AssertionError("keyed recovery must not search")

    mutation = type("M", (), {"payload": {"local_id": "wa-abort"}, "target": "wa-abort"})()
    outcome = route_mod.run_coordinated_outbound_create(
        mutation, client=_LabelFails(), binding_store=store
    )

    assert outcome.disposition.value == "safety_aborted"
    assert outcome.dependents_released is False
    assert outcome.known_key == "DIG-901"
    # A keyed-pending binding remains (key recorded, NOT confirmed) — no delete.
    assert store.get_jira_key("wa-abort") == "DIG-901"

    # Deterministic keyed recovery: confirm + retro-attach with NO search.
    recovery_client = MagicMock()
    recovered = store.recover_pending_bindings(recovery_client)
    assert recovered == 1
    recovery_client.search_issues.assert_not_called()
    recovery_client.add_label.assert_called_once_with("DIG-901", "rebar-id:wa-abort")
    assert "wa-abort" not in store.pending_bindings()
