"""Happy-path contract for the committed GET-rotation sidecar."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BINDING_STORE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "binding_store.py"
)


def _load_binding_store_module():
    name = "_test_get_rotation_binding_store"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, BINDING_STORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


def test_binding_store_reads_max_and_dual_writes_rotation_stamp(tmp_path: Path) -> None:
    """New readers take max(sidecar, legacy); new writes advance both formats."""
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
    assert bindings["bindings"]["loc-a"]["last_get_pass"] == "2026-07-01T00-00-04"
    assert rotation["last_get_pass"]["DIG-A"] == "2026-07-01T00-00-04"
    assert bindings["bindings"]["loc-b"]["last_get_pass"] == "2026-07-01T00-00-02"
    assert rotation["last_get_pass"]["DIG-B"] == "2026-07-01T00-00-03"
