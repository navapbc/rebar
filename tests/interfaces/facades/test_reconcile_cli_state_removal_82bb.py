"""Held-out oracle for removing legacy reconcile CLI/state compatibility."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from rebar import _bridge_runner
from rebar._cli import _registry

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER = REPO_ROOT / "src" / "rebar" / "reconciler"


def test_legacy_reconcile_cli_is_absent_and_engine_filter_is_retained() -> None:
    mode_mod = importlib.import_module("rebar.reconciler.mode")
    request_mod = importlib.import_module("rebar.reconciler.request")

    assert _registry.route_for("reconcile") is None
    with pytest.raises((request_mod.RequestError, ValueError)):
        request_mod.normalize_request(["--mode", "reconcile-check"], mode_mod)
    request = request_mod.normalize_request(["--filter-local-ids", "local-1"], mode_mod)
    assert request.route == "legacy"
    assert request.filter_local_ids == {"local-1"}


def test_bridge_runner_dry_run_profile_invokes_canonical_preview() -> None:
    assert "reconcile-check" not in _bridge_runner.MODE_ARGS
    assert _bridge_runner.MODE_ARGS["dry-run"] == ("preview",)


def test_bridge_status_ignores_retired_reconcile_check_artifact(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    last_pass = importlib.import_module("rebar.reconciler.last_pass")
    store = rebar_repo / ".tickets-tracker"
    state_dir = store / ".bridge_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "reconcile-check.json").write_text(
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
    # The MCP bridge_status outputSchema IS bridge_status.schema.json (story
    # quilted-nepotic-kitty), so the schema assertion below covers the MCP surface too.
    assert "reconcile_diagnostics" not in bridge_status_schema["properties"]


def test_retired_pause_sentinel_blocks_write_capable_modes(rebar_repo: Path) -> None:
    mode_mod = importlib.import_module("rebar.reconciler.mode")
    _advisory_lock = importlib.import_module("rebar.reconciler._advisory_lock")
    ref_lock = _advisory_lock._load_ref_lock()

    ref_lock.set_gate(rebar_repo, "reconcile-check")

    assert ref_lock.read_gate(rebar_repo) == "reconcile-check"
    for write_mode in (
        mode_mod.Mode.BOOTSTRAP_STRICT,
        mode_mod.Mode.BOOTSTRAP_THROTTLE,
        mode_mod.Mode.LIVE,
    ):
        assert _advisory_lock.check_phase_gate(write_mode, rebar_repo) is True
    assert _advisory_lock.check_phase_gate(mode_mod.Mode.DRY_RUN, rebar_repo) is False
