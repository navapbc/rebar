"""Acceptance coverage for retiring the live reconcile-check compatibility vocabulary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

LIVE_SURFACES = (
    Path("src/rebar/_engine/rebar_reconciler/_preflight.py"),
    Path("src/rebar/_engine/rebar_reconciler/binding_recovery.py"),
    Path("src/rebar/_engine/rebar_reconciler/binding_walk.py"),
    Path("src/rebar/_engine/rebar_reconciler/fetcher.py"),
    Path("src/rebar/_engine/rebar_reconciler/get_rotation.py"),
    Path("src/rebar/_engine/rebar_reconciler/load_phase.py"),
    Path("src/rebar/_engine/rebar_reconciler/pass_support.py"),
    Path("src/rebar/_engine/rebar_reconciler/reconcile_helpers.py"),
    Path("src/rebar/_engine/rebar_reconciler/ticket_planner.py"),
    Path("src/rebar/_bridge_runner.py"),
    Path("src/rebar/schemas/bridge_fsck.schema.json"),
    Path(".github/workflows/reconcile-bridge.yml"),
    Path("Jenkinsfile"),
)


def _bridge_runner_modes() -> dict[str, tuple[str, ...]]:
    path = ROOT / "src" / "rebar" / "_bridge_runner.py"
    spec = importlib.util.spec_from_file_location("bridge_runner_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MODE_COMMANDS


def _mode_module():
    path = ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mode.py"
    spec = importlib.util.spec_from_file_location("mode_under_vocabulary_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_surfaces_drop_legacy_vocabulary_and_keep_preview_profile() -> None:
    mode = _mode_module()
    violations = {
        str(path): token
        for path in LIVE_SURFACES
        for token in ("reconcile-check", "RECONCILE_CHECK")
        if token in (ROOT / path).read_text(encoding="utf-8")
    }
    if _bridge_runner_modes().get("dry-run") != ("rebar", "bridge", "preview"):
        violations["src/rebar/_bridge_runner.py"] = "dry-run preview route missing"
    if any(
        member.name == "RECONCILE_CHECK" or member.value == "reconcile-check"
        for member in mode.Mode
    ):
        violations["src/rebar/_engine/rebar_reconciler/mode.py"] = "live mode member retained"
    if any(member.value == "reconcile-check" for member in mode.MODE_CAPS):
        violations["src/rebar/_engine/rebar_reconciler/mode.py"] = "live mode cap retained"
    if mode.Mode.from_str("reconcile-check") is not mode.Mode.DRY_RUN:
        violations["src/rebar/_engine/rebar_reconciler/mode.py"] = "historical decoder missing"
    with pytest.raises(ValueError) as exc_info:
        mode.Mode.from_str("not-a-mode")
    if "reconcile-check" in str(exc_info.value):
        violations["src/rebar/_engine/rebar_reconciler/mode.py"] = "legacy alias advertised"

    assert violations == {}
