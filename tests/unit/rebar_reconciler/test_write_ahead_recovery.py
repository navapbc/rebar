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
    """A keyless-pending record whose label search misses is unbound (create never landed)."""
    store = _new_store(binding_store_mod, tmp_path)
    store.bind_pending("local-C")

    client = MagicMock()
    client.search_issues.return_value = []
    recovered = store.recover_pending_bindings(client)

    assert recovered == 1
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
    store.save.side_effect = OSError("disk full")  # the pre-create persist fails

    mutation = {
        "local_id": "wa-2",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
    }
    with pytest.raises(dispatch.BindingPersistError):
        dispatch.create_one(mutation, client, repo_root=tmp_path, binding_store=store)

    client.create_issue.assert_not_called()
