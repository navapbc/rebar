"""Real-git oracle for a sidecar-only binding-store commit."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"


def _load_reconcile():
    name = "_test_get_rotation_commit_reconcile"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, RECONCILE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git(tracker: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tracker), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_sidecar_only_change_is_staged_and_committed(tmp_path: Path) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "tickets", str(tracker)],
        check=True,
        capture_output=True,
    )
    _git(tracker, "config", "user.email", "test@example.com")
    _git(tracker, "config", "user.name", "Test")

    bridge = tracker / ".bridge_state"
    bridge.mkdir()
    bindings = bridge / "bindings.json"
    rotation = bridge / "get_rotation.json"
    bindings.write_text(json.dumps({"version": 2, "bindings": {}, "reverse": {}}))
    rotation.write_text(json.dumps({"version": 1, "last_get_pass": {"DIG-1": "p1"}}))
    _git(tracker, "add", ".bridge_state/bindings.json", ".bridge_state/get_rotation.json")
    _git(tracker, "commit", "--no-verify", "-m", "initial rotation state")
    before = _git(tracker, "rev-parse", "HEAD")
    bindings_before = bindings.read_bytes()

    rotation.write_text(json.dumps({"version": 1, "last_get_pass": {"DIG-1": "p2"}}))
    reconcile = _load_reconcile()
    assert reconcile._commit_binding_store_snapshot(object(), tmp_path, "rotation-only") is True

    after = _git(tracker, "rev-parse", "HEAD")
    assert after != before
    assert bindings.read_bytes() == bindings_before
    assert _git(tracker, "diff", "--name-only", "HEAD^", "HEAD") == (
        ".bridge_state/get_rotation.json"
    )
    committed = json.loads(_git(tracker, "show", "HEAD:.bridge_state/get_rotation.json"))
    assert committed["last_get_pass"] == {"DIG-1": "p2"}
    assert _git(tracker, "status", "--porcelain") == ""
