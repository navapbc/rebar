"""Story 9622 (D1): deterministic write-ahead + pending-binding recovery.

Write-ahead: a durable pending record is persisted BEFORE create_issue; the Jira
key is recorded on the still-pending entry (and persisted) BEFORE the rebar-id
label. Recovery is then deterministic:

- keyed-pending (key already recorded)  -> confirm + retro-attach the label, NO Jira
  search  -> a crash between create and label yields NO duplicate.
- keyless-pending                        -> search the rebar-id label; confirm if
  found, else unbind (the create never reached Jira).
- bind_pending persist failure           -> BindingPersistError, create_issue is
  NOT called (item-scoped skip; recorded failed upstream).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("binding_store_wa_test", "binding_store.py")


@pytest.fixture(scope="module")
def dispatch():
    return _load("dispatch_one_wa_test", "dispatch_one.py")


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
    """A keyless-pending record whose label search misses is unbound — but only once
    absence is CORROBORATED.

    This test previously asserted the unbind after a SINGLE negative search. That
    assertion encoded bug 21fc: the keyless-pending state is entered exactly when we
    crashed during create_issue, and Jira DC's Lucene index is eventually consistent
    (JRASERVER-70423: a 2,991s lag observed), so one empty search is precisely what a
    LIVE issue looks like — and unbinding on it made the next pass write a DUPLICATE.

    The intent is unchanged and still asserted: a truly-absent issue must not strand its
    ticket pending forever. What changed is that absence must now be corroborated by
    repeated misses AND an entry older than the index-lag grace window. This is the
    sibling of ``test_recover_pending_not_found_in_jira`` in
    ``state/test_binding_store.py``, which pinned the same defect.
    """
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

    # The unbind lands on the THIRD miss overall (one above + the loop's), then there is
    # nothing left to resolve — so assert exactly one resolution across the sequence
    # rather than pinning which call it fell on.
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
    # A bare MagicMock answers EVERY member with a truthy Mock, including the
    # keyless-pending grace gate create_one consults (21fc) — which would defer this
    # create and leave `order` empty. A real store returns False here.
    store.is_keyless_pending_within_grace.return_value = False
    store.bind_pending.side_effect = lambda *a, **k: order.append("bind_pending")
    store.record_pending_key.side_effect = lambda *a, **k: order.append("record_key")
    store.save.side_effect = lambda *a, **k: order.append("save")

    mutation = {
        "local_id": "wa-1",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
    }
    dispatch.create_one(mutation, client, repo_root=tmp_path, binding_store=store)

    # bind_pending + its save come before create; record_key + its save before label.
    assert order.index("bind_pending") < order.index("create")
    assert order.index("record_key") < order.index("add_label")
    assert order.index("create") < order.index("record_key")


def test_bind_pending_persist_failure_skips_create(dispatch, tmp_path):
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
    with pytest.raises(dispatch.BindingPersistError):
        dispatch.create_one(mutation, client, repo_root=tmp_path, binding_store=store)

    client.create_issue.assert_not_called()


# --------------------------------------------------------------------------- #
# S4 T3 (2863-c335): the coordinated create route preserves the write-ahead
# protocol AND its deterministic keyed recovery (AC2).
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def route_mod():
    return _load("create_route_wa_test", "create_route.py")


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
    """AC2: when the coordinated route aborts AFTER recording the key (label write
    fails), it leaves a KEYED-pending binding and NEVER deletes; the subsequent
    recovery confirms it with ZERO Jira search."""
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
