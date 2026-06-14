"""Unit tests for BindingStore absence lifecycle (bug 1e08-1a35-0267-4ca6).

Covers note_absent / clear_absent / set_last_get / is_retired, retirement at
GRACE consecutive 404s, the fail-OPEN load of bindings-retired.json, and the
defensive env-var parse for the lifecycle knobs.

Follows the importlib loader convention established across this test tree.
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


def _load():
    spec = importlib.util.spec_from_file_location("binding_store_1e08", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load()
BindingStore = _mod.BindingStore


@pytest.fixture()
def tracker(tmp_path: Path) -> Path:
    """Return a repo_root whose .tickets-tracker holds a confirmed binding."""
    return tmp_path


def _store_with_binding(
    repo_root: Path, local_id: str, jira_key: str
) -> BindingStore:
    bs = BindingStore(repo_root / ".tickets-tracker")
    bs.bind_confirm(local_id, jira_key)
    bs.save()
    return bs


# --------------------------------------------------------------------------
# #9 — single 200 resets the counter (no premature retirement of a flapper)
# --------------------------------------------------------------------------


def test_clear_absent_resets_counter(tracker: Path) -> None:
    bs = _store_with_binding(tracker, "loc-1", "DIG-1")
    bs.note_absent("DIG-1")
    bs.note_absent("DIG-1")
    bs.clear_absent("DIG-1")
    # After reset, two more 404s must NOT retire (counter restarted from 0).
    bs.note_absent("DIG-1")
    bs.note_absent("DIG-1")
    assert not bs.is_retired("DIG-1"), (
        "A single 200 GET must reset the consecutive-404 counter; a flapping "
        "issue must not retire after clear_absent."
    )
    assert bs.get_jira_key("loc-1") == "DIG-1"


# --------------------------------------------------------------------------
# #2 — retire after GRACE consecutive 404s
# --------------------------------------------------------------------------


def test_retires_after_grace_consecutive_404s(tracker: Path) -> None:
    bs = _store_with_binding(tracker, "loc-2", "DIG-2")
    # Default GRACE=3
    bs.note_absent("DIG-2")
    bs.note_absent("DIG-2")
    assert not bs.is_retired("DIG-2"), "Must not retire before GRACE 404s"
    bs.note_absent("DIG-2")
    assert bs.is_retired("DIG-2"), "Must retire at GRACE consecutive 404s"
    # Soft-delete: live binding removed.
    assert bs.get_jira_key("loc-2") is None
    # Reversible: present in retired file.
    retired_path = (
        tracker / ".tickets-tracker" / ".bridge_state" / "bindings-retired.json"
    )
    assert retired_path.exists()
    data = json.loads(retired_path.read_text())
    assert "DIG-2" in data["retired"]
    assert data["retired"]["DIG-2"]["local_id"] == "loc-2"


def test_retirement_persists_across_reload(tracker: Path) -> None:
    bs = _store_with_binding(tracker, "loc-3", "DIG-3")
    for _ in range(3):
        bs.note_absent("DIG-3")
    assert bs.is_retired("DIG-3")
    # Fresh store sees the retirement (loaded from bindings-retired.json).
    bs2 = BindingStore(tracker / ".tickets-tracker")
    assert bs2.is_retired("DIG-3")


# --------------------------------------------------------------------------
# set_last_get / last_get_pass rotation bookkeeping
# --------------------------------------------------------------------------


def test_set_last_get_records_pass_id(tracker: Path) -> None:
    bs = _store_with_binding(tracker, "loc-4", "DIG-4")
    assert bs.last_get_pass("DIG-4") == "", "Never-GET'd key returns '' sentinel"
    bs.set_last_get("DIG-4", "2026-06-05T09-31-46")
    assert bs.last_get_pass("DIG-4") == "2026-06-05T09-31-46"
    bs.save()
    bs2 = BindingStore(tracker / ".tickets-tracker")
    assert bs2.last_get_pass("DIG-4") == "2026-06-05T09-31-46"


# --------------------------------------------------------------------------
# #12 — corrupt bindings-retired.json → fail-OPEN (empty set + alert)
# --------------------------------------------------------------------------


def test_corrupt_retired_file_fails_open(tracker: Path) -> None:
    bridge = tracker / ".tickets-tracker" / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings-retired.json").write_text("{ this is not valid json ")
    # Must NOT raise — degrades to empty retired-set.
    bs = BindingStore(tracker / ".tickets-tracker")
    assert bs.is_retired("DIG-X") is False
    # Alert emitted to bridge_alerts.
    alerts_dir = tracker / "bridge_state" / "bridge_alerts"
    assert alerts_dir.is_dir(), "fail-open must emit an alert"
    blob = "".join(p.read_text() for p in alerts_dir.glob("*.jsonl"))
    assert "binding-retired-file-corrupt" in blob


def test_corrupt_bindings_json_still_fails_closed(tracker: Path) -> None:
    bridge = tracker / ".tickets-tracker" / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text("{ corrupt ")
    with pytest.raises(ValueError):
        BindingStore(tracker / ".tickets-tracker")


# --------------------------------------------------------------------------
# Defensive env-var parse
# --------------------------------------------------------------------------


def test_malformed_grace_env_falls_back_to_default(tracker: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "not-an-int")
    bs = _store_with_binding(tracker, "loc-5", "DIG-5")
    bs.note_absent("DIG-5")
    bs.note_absent("DIG-5")
    assert not bs.is_retired("DIG-5"), "default GRACE=3 applies on malformed value"
    bs.note_absent("DIG-5")
    assert bs.is_retired("DIG-5")


def test_grace_env_override(tracker: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECONCILER_ABSENT_RETIRE_GRACE", "1")
    bs = _store_with_binding(tracker, "loc-6", "DIG-6")
    bs.note_absent("DIG-6")
    assert bs.is_retired("DIG-6"), "GRACE=1 retires after a single 404"
