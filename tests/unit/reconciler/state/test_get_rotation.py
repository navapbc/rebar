"""Happy-path contract for the committed GET-rotation sidecar."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BINDING_STORE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "binding_store.py"


def _load_binding_store_module():
    return importlib.import_module("rebar.reconciler.binding_store")


def _write_bindings(bridge: Path) -> None:
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": {
                    "loc-a": {
                        "jira_key": "DIG-A",
                        "state": "confirmed",
                        "last_get_pass": "2026-07-01T00-00-02",
                    },
                    "loc-b": {
                        "jira_key": "DIG-B",
                        "state": "confirmed",
                        "last_get_pass": "2026-07-01T00-00-02",
                    },
                },
                "reverse": {"DIG-A": "loc-a", "DIG-B": "loc-b"},
            }
        )
    )
    (bridge / "get_rotation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "last_get_pass": {
                    "DIG-A": "2026-07-01T00-00-01",
                    "DIG-B": "2026-07-01T00-00-03",
                },
            }
        )
    )


def test_binding_store_reads_max_and_migrates_to_sidecar_only(tmp_path: Path) -> None:
    """A successful new-writer save persists the max and scrubs legacy stamps."""
    tracker = tmp_path / ".tickets-tracker"
    bridge = tracker / ".bridge_state"
    _write_bindings(bridge)

    module = _load_binding_store_module()
    store = module.BindingStore(tracker)

    assert store.last_get_pass("DIG-A") == "2026-07-01T00-00-02"
    assert store.last_get_pass("DIG-B") == "2026-07-01T00-00-03"

    store.set_last_get("DIG-A", "2026-07-01T00-00-04")
    store.save()

    bindings = json.loads((bridge / "bindings.json").read_text())
    rotation = json.loads((bridge / "get_rotation.json").read_text())
    assert rotation["last_get_pass"]["DIG-A"] == "2026-07-01T00-00-04"
    assert rotation["last_get_pass"]["DIG-B"] == "2026-07-01T00-00-03"
    assert all("last_get_pass" not in entry for entry in bindings["bindings"].values())
    assert bindings["bindings"]["loc-a"] == {
        "jira_key": "DIG-A",
        "state": "confirmed",
    }
    assert bindings["bindings"]["loc-b"] == {
        "jira_key": "DIG-B",
        "state": "confirmed",
    }

    reloaded = module.BindingStore(tracker)
    assert reloaded.last_get_pass("DIG-A") == "2026-07-01T00-00-04"
    assert reloaded.last_get_pass("DIG-B") == "2026-07-01T00-00-03"
