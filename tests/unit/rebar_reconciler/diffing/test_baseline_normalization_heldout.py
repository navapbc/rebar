"""A3 held-out contract oracle: normalized baseline storage is read-equivalent.

These tests assert only observable store and outbound-mutation behavior. They are
physically withheld from the implementation worker until held-out validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar_reconciler.adapters.jira.backend import JiraBackend
from rebar_reconciler.binding_store import BindingStore
from rebar_reconciler.outbound_field_diff import compute_update_fields

pytestmark = pytest.mark.unit

_ADF_BASELINE = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "baseline body"}],
        }
    ],
}


def _store(tmp_path: Path, name: str, baseline: dict) -> BindingStore:
    store = BindingStore(tmp_path / name)
    store.bind_confirm("loc-1", "REB-1")
    store.set_baseline("loc-1", baseline)
    return store


def _ticket(*, assignee: str = "") -> dict:
    return {
        "ticket_id": "loc-1",
        "title": "Same title",
        "description": "baseline body",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": assignee,
    }


def _remote(*, assignee: object = None) -> dict:
    return {
        "summary": "Same title",
        "description": _ADF_BASELINE,
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": assignee,
    }


def _fields(store: BindingStore, *, assignee: str = "", remote_assignee: object = None) -> dict:
    backend = JiraBackend(transport=object())
    return compute_update_fields(
        _ticket(assignee=assignee),
        _remote(assignee=remote_assignee),
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
        binding_store=store,
        local_id="loc-1",
        jira_key="REB-1",
    )


def test_dict_and_scalar_baselines_have_identical_live_update_verdicts(tmp_path: Path) -> None:
    vendor = _store(
        tmp_path,
        "vendor",
        {
            "description": _ADF_BASELINE,
            "priority": {"id": "2", "name": "Medium"},
            "status": {"id": "1", "name": "To Do"},
        },
    )
    scalar = _store(
        tmp_path,
        "scalar",
        {"description": "baseline body", "priority": "Medium", "status": "To Do"},
    )

    assert _fields(vendor) == _fields(scalar) == {}
    assert vendor.get_baseline("loc-1") == scalar.get_baseline("loc-1")


@pytest.mark.parametrize("local_identity", ["ada@example.test", "acct-ada", "Ada Lovelace"])
def test_assignee_identity_shape_remains_available_to_direction_suppression(
    tmp_path: Path, local_identity: str
) -> None:
    baseline_assignee = {
        "displayName": "Ada Lovelace",
        "emailAddress": "ada@example.test",
        "accountId": "acct-ada",
    }
    store = _store(
        tmp_path,
        local_identity.replace("@", "-"),
        {
            "description": _ADF_BASELINE,
            "assignee": baseline_assignee,
        },
    )
    remote_assignee = {
        "displayName": "Grace Hopper",
        "emailAddress": "grace@example.test",
        "accountId": "acct-grace",
    }

    # Local still matches one form of the baseline identity while Jira changed:
    # Jira wins directionality, so no outbound assignee update is emitted.
    assert _fields(store, assignee=local_identity, remote_assignee=remote_assignee) == {}
    assert store.get_baseline("loc-1")["description"] == "baseline body"
    assert store.get_baseline("loc-1")["assignee"] == baseline_assignee


def test_already_scalar_and_partial_inputs_are_idempotent(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        "partial",
        {
            "description": _ADF_BASELINE,
            "status": {"id": "9", "name": "Done"},
            "priority": {"id": "4", "name": "Low"},
        },
    )
    first = store.get_baseline("loc-1")
    assert first == {
        "description": "baseline body",
        "status": "Done",
        "priority": "Low",
    }

    store.set_baseline(
        "loc-1",
        {"description": "baseline body", "status": "Done", "priority": "Low"},
    )

    assert store.get_baseline("loc-1") == first
    assert "summary" not in first
    assert "assignee" not in first
