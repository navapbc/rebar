"""Held-out oracle for removing legacy reconcile CLI/state compatibility."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from rebar import _bridge_runner
from rebar._cli import _registry
from rebar._mcp_models import BridgeStatusOut

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECONCILER / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_legacy_reconcile_cli_and_filter_flag_are_absent() -> None:
    mode_mod = _load_module("heldout_82bb_mode", "mode.py")
    request_mod = _load_module("heldout_82bb_request", "request.py")

    assert _registry.route_for("reconcile") is None
    with pytest.raises((request_mod.RequestError, ValueError)):
        request_mod.normalize_request(["--mode", "reconcile-check"], mode_mod)
    with pytest.raises(request_mod.RequestError):
        request_mod.normalize_request(["--filter-local-ids", "local-1"], mode_mod)


def test_bridge_runner_reconcile_check_profile_invokes_canonical_preview() -> None:
    assert _bridge_runner.MODE_COMMANDS["reconcile-check"] == ("rebar", "bridge", "preview")


def test_bridge_status_ignores_retired_reconcile_check_artifact(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    last_pass = _load_module("heldout_82bb_last_pass", "last_pass.py")
    store = rebar_repo / ".tickets-tracker"
    bridge_state = store / ".bridge_state"
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "reconcile-check.json").write_text(
        json.dumps(
            {
                "total_bindings": 1,
                "checked": 1,
                "in_sync": 0,
                "discrepancies": [{"jira_key": "REB-1", "local_id": "local-1"}],
                "orphaned_bindings": [],
                "orphaned_jira": [],
                "unbound_local": 0,
                "unbound_jira": 0,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    status = last_pass.snapshot(rebar_repo)
    bridge_status_schema = json.loads(
        (REPO_ROOT / "src" / "rebar" / "schemas" / "bridge_status.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert "reconcile_diagnostics" not in status
    assert "reconcile_diagnostics" not in BridgeStatusOut.model_fields
    assert "reconcile_diagnostics" not in bridge_status_schema["properties"]


def test_reconcile_check_pause_sentinel_still_blocks_higher_modes(rebar_repo: Path) -> None:
    mode_mod = _load_module("heldout_82bb_mode_pause", "mode.py")
    _advisory_lock = _load_module("heldout_82bb_advisory_lock", "_advisory_lock.py")
    ref_lock = _advisory_lock._load_ref_lock()

    ref_lock.set_gate(rebar_repo, mode_mod.Mode.RECONCILE_CHECK.value)

    assert ref_lock.read_gate(rebar_repo) == "reconcile-check"
    assert _advisory_lock.check_phase_gate(mode_mod.Mode.BOOTSTRAP_THROTTLE, rebar_repo) is True
    assert _advisory_lock.check_phase_gate(mode_mod.Mode.RECONCILE_CHECK, rebar_repo) is False
