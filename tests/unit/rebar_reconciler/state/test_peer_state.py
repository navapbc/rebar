"""Direct unit tests for the extracted last-synced-peer-state module (ticket 4522).

Exercises ``rebar_reconciler.peer_state`` WITHOUT constructing a ``BindingStore`` —
the point of the extraction — and pins the two absence-semantics properties that
make the cluster safe (a refactor is exactly when they get quietly dropped):

- an ABSENT baseline is VALID and degrades to local-wins (``get_baseline`` → None,
  ADR 0026 §2);
- an ABSENT peer-parent observation is VALID and fails safe to NO clear
  (``get_peer_parent`` → None, ticket 88d9).
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler import peer_state

_FIELDS = {
    "summary": "S",
    "description": "D",
    "priority": "High",
    "status": "In Progress",
    "assignee": "a@x",
    "extraneous": "dropped",  # not a mirrored field — must be filtered out
}


def _bound(jira_key: str = "DIG-1") -> dict[str, Any]:
    return {"jira_key": jira_key, "status": "confirmed"}


# -- absence semantics: absent baseline degrades to local-wins (None) ---------


def test_absent_baseline_is_valid_and_returns_none() -> None:
    bindings = {"t1": _bound()}  # entry predates baselines: no "baseline" key
    assert peer_state.get_baseline(bindings, "t1") is None


def test_unbound_id_baseline_returns_none() -> None:
    assert peer_state.get_baseline({}, "missing") is None


def test_non_dict_baseline_degrades_to_none() -> None:
    bindings = {"t1": {**_bound(), "baseline": "corrupt"}}
    assert peer_state.get_baseline(bindings, "t1") is None


# -- absence semantics: absent peer parent fails safe to NO clear (None) ------


def test_absent_peer_parent_is_valid_and_returns_none() -> None:
    bindings = {"t1": _bound()}  # pre-field binding: no "peer_parent" key
    assert peer_state.get_peer_parent(bindings, "t1") is None


def test_unbound_id_peer_parent_returns_none() -> None:
    assert peer_state.get_peer_parent({}, "missing") is None


def test_observed_no_parent_still_presents_none() -> None:
    """'Observed to have none' ('') and 'never observed' (absent) both read None."""
    bindings = {"t1": _bound()}
    peer_state.set_peer_parent(bindings, "t1", None)
    assert bindings["t1"]["peer_parent"] == ""
    assert peer_state.get_peer_parent(bindings, "t1") is None


# -- round-trips, filtering, no-op guards --------------------------------------


def test_set_get_baseline_roundtrip_filters_to_mirrored_fields() -> None:
    bindings = {"t1": _bound()}
    peer_state.set_baseline(bindings, "t1", _FIELDS)
    got = peer_state.get_baseline(bindings, "t1")
    assert got is not None
    assert set(got) == set(peer_state._BASELINE_FIELDS)
    assert "extraneous" not in got


def test_get_baseline_returns_a_copy() -> None:
    bindings = {"t1": _bound()}
    peer_state.set_baseline(bindings, "t1", _FIELDS)
    first = peer_state.get_baseline(bindings, "t1")
    assert first is not None
    first["summary"] = "mutated"
    second = peer_state.get_baseline(bindings, "t1")
    assert second is not None
    assert second["summary"] == "S"


def test_set_baseline_on_unbound_id_is_noop() -> None:
    bindings: dict[str, Any] = {}
    peer_state.set_baseline(bindings, "missing", _FIELDS)
    assert bindings == {}


def test_set_peer_parent_on_unbound_id_is_noop() -> None:
    bindings: dict[str, Any] = {}
    peer_state.set_peer_parent(bindings, "missing", "DIG-9")
    assert bindings == {}


def test_set_get_peer_parent_roundtrip() -> None:
    bindings = {"t1": _bound()}
    peer_state.set_peer_parent(bindings, "t1", "DIG-9")
    assert peer_state.get_peer_parent(bindings, "t1") == "DIG-9"


def test_seed_baselines_from_snapshot_counts_only_bound_present_keys() -> None:
    bindings = {
        "t1": _bound("DIG-1"),
        "t2": _bound("DIG-2"),
        "t3": {"status": "pending"},  # keyless-pending: never seeded
    }
    snapshot = {"DIG-1": dict(_FIELDS)}  # DIG-2 absent from the snapshot
    assert peer_state.seed_baselines_from_snapshot(bindings, snapshot) == 1
    assert peer_state.get_baseline(bindings, "t1") is not None
    assert peer_state.get_baseline(bindings, "t2") is None
    assert peer_state.get_baseline(bindings, "t3") is None
