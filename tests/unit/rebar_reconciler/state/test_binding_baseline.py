"""Per-binding baseline store round-trip + version-compat + fail-closed (ADR 0026).

Foundation ticket baf7: the three-way-merge baseline lives on the binding entry.
Covers get/set/seed, absent-baseline-degrades-to-None (local-wins), version-1
back-compat read, and the preserved corrupt-bindings.json fail-closed guard.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "binding_store.py"
)
_spec = importlib.util.spec_from_file_location("binding_store_baseline", _SRC)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

BindingStore = _mod.BindingStore


def _store(tmp_path: Path) -> BindingStore:
    return BindingStore(tmp_path / ".tickets-tracker")


_FIELDS = {
    "summary": "S",
    "description": "D",
    "priority": "High",
    "status": "In Progress",
    "assignee": "a@x",
    "extraneous": "dropped",  # not a mirrored field — must be filtered out
}

_ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "hello baseline"}],
        }
    ],
}


def test_set_and_get_baseline_round_trip(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    s.set_baseline("loc-1", _FIELDS)
    got = s.get_baseline("loc-1")
    assert got == {
        "summary": "S",
        "description": "D",
        "priority": "High",
        "status": "In Progress",
        "assignee": "a@x",
    }
    assert "baseline_advanced_at" not in s.all_bindings()["loc-1"]


def test_set_baseline_normalizes_vendor_text_shapes_but_keeps_assignee_raw(tmp_path):
    """A3 happy path: shrink only the three text-like vendor-shaped values."""
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    assignee = {
        "displayName": "Ada Lovelace",
        "emailAddress": "ada@example.test",
        "accountId": "acct-ada",
    }

    s.set_baseline(
        "loc-1",
        {
            "summary": "S",
            "description": _ADF_DESCRIPTION,
            "priority": {"id": "1", "name": "High"},
            "status": {"id": "3", "name": "In Progress"},
            "assignee": assignee,
        },
    )

    assert s.get_baseline("loc-1") == {
        "summary": "S",
        "description": "hello baseline",
        "priority": "High",
        "status": "In Progress",
        "assignee": assignee,
    }


def test_unchanged_baseline_keeps_serialized_bytes_identical(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    monkeypatch.setattr(
        _mod,
        "_now_iso",
        lambda: pytest.fail("setting a baseline must not read the clock"),
    )

    s.set_baseline("loc-1", _FIELDS)
    s.save()
    bindings_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json"
    first_bytes = bindings_path.read_bytes()

    s.set_baseline("loc-1", dict(_FIELDS))
    s.save()

    assert bindings_path.read_bytes() == first_bytes


def test_baseline_persists_across_reload(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    s.set_baseline("loc-1", _FIELDS)
    s.save()
    reloaded = _store(tmp_path)
    assert reloaded.get_baseline("loc-1")["status"] == "In Progress"
    # New stores are written at version 2.
    data = json.loads(
        (tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json").read_text()
    )
    assert data["version"] == 2


def test_absent_baseline_returns_none(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    # No set_baseline → None (degrades to local-wins per ADR 0026 §2).
    assert s.get_baseline("loc-1") is None
    # Unbound id → None.
    assert s.get_baseline("nope") is None


def test_set_baseline_noop_when_unbound(tmp_path):
    s = _store(tmp_path)
    s.set_baseline("not-bound", _FIELDS)  # must not raise, must not create
    assert s.get_baseline("not-bound") is None
    assert "not-bound" not in s.all_bindings()


def test_version_1_store_reads_without_baseline(tmp_path):
    """A legacy version-1 bindings.json (no baselines) is VALID and reads clean."""
    state_dir = tmp_path / ".tickets-tracker" / ".bridge_state"
    state_dir.mkdir(parents=True)
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {"loc-1": {"jira_key": "REB-1", "state": "confirmed"}},
                "reverse": {"REB-1": "loc-1"},
            }
        )
    )
    s = _store(tmp_path)
    assert s.get_baseline("loc-1") is None  # absent → local-wins, not an error
    assert s.is_bound("loc-1")


def test_seed_baselines_from_snapshot(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    s.bind_confirm("loc-2", "REB-2")
    s.bind_confirm("loc-3", "REB-3")  # not in snapshot → not seeded
    prev_snapshot = {
        "REB-1": {
            "summary": "one",
            "status": "To Do",
            "priority": "Low",
            "description": "d1",
            "assignee": "",
        },
        "REB-2": {
            "summary": "two",
            "status": "Done",
            "priority": "High",
            "description": "d2",
            "assignee": "x@y",
        },
    }
    seeded = s.seed_baselines_from_snapshot(prev_snapshot)
    assert seeded == 2
    assert s.get_baseline("loc-1")["summary"] == "one"
    assert s.get_baseline("loc-2")["status"] == "Done"
    assert s.get_baseline("loc-3") is None


def test_changed_baseline_updates_serialized_bytes(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    s.set_baseline("loc-1", _FIELDS)
    s.save()
    bindings_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json"
    first_bytes = bindings_path.read_bytes()

    changed = {**_FIELDS, "status": "Done"}
    s.set_baseline("loc-1", changed)
    s.save()

    assert bindings_path.read_bytes() != first_bytes
    assert _store(tmp_path).get_baseline("loc-1")["status"] == "Done"
    assert "baseline_advanced_at" not in s.all_bindings()["loc-1"]


def test_present_null_description_uses_exact_normalizer_projection(tmp_path):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")

    s.set_baseline("loc-1", {"description": None})

    baseline = s.get_baseline("loc-1")
    assert "description" in baseline
    assert baseline["description"] == ""
    assert "baseline_advanced_at" not in s.all_bindings()["loc-1"]


def test_seed_baselines_unchanged_keeps_serialized_bytes_identical(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.bind_confirm("loc-1", "REB-1")
    monkeypatch.setattr(
        _mod,
        "_now_iso",
        lambda: pytest.fail("seeding a baseline must not read the clock"),
    )
    snapshot = {"REB-1": dict(_FIELDS)}

    assert s.seed_baselines_from_snapshot(snapshot) == 1
    s.save()
    bindings_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json"
    first_bytes = bindings_path.read_bytes()

    assert s.seed_baselines_from_snapshot(snapshot) == 1
    s.save()

    assert bindings_path.read_bytes() == first_bytes


def test_corrupt_bindings_json_still_fails_closed(tmp_path):
    """The baseline extension must NOT weaken the fail-closed corruption guard."""
    state_dir = tmp_path / ".tickets-tracker" / ".bridge_state"
    state_dir.mkdir(parents=True)
    (state_dir / "bindings.json").write_text("<<<<<<< HEAD\n{not valid json")
    with pytest.raises(ValueError, match="corrupt or contains git conflict"):
        _store(tmp_path)
