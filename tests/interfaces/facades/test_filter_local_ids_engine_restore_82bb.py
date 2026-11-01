"""Held-out contract for retaining engine-only ``--filter-local-ids``."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER = REPO_ROOT / "src" / "rebar" / "reconciler"


def test_legacy_engine_route_accepts_filter_local_ids_post_filter() -> None:
    mode_mod = importlib.import_module("rebar.reconciler.mode")
    request_mod = importlib.import_module("rebar.reconciler.request")
    pass_support = importlib.import_module("rebar.reconciler.pass_support")

    request = request_mod.normalize_request(
        [
            "--mode",
            "bootstrap-strict",
            "--filter-local-ids",
            "local-1,DC-123",
        ],
        mode_mod,
    )

    assert request.route == "legacy"
    assert request.target_mode == mode_mod.Mode.BOOTSTRAP_STRICT
    assert request.selection_tokens == ()
    assert request.filter_local_ids == {"local-1", "DC-123"}

    target_set = pass_support._build_filter_target_set(
        request.filter_local_ids,
        SimpleNamespace(get_jira_key=lambda _local_id: None),
    )
    inbound_create = SimpleNamespace(
        target="DC-123",
        provenance={"jira_key": "DC-123", "local_id": "derived-but-not-yet-local"},
    )
    assert pass_support._mutation_matches_filter(inbound_create, target_set)


@pytest.mark.parametrize("command", ["preview", "sync"])
def test_primary_bridge_commands_reject_filter_local_ids(command: str) -> None:
    mode_mod = importlib.import_module("rebar.reconciler.mode")
    request_mod = importlib.import_module("rebar.reconciler.request")

    with pytest.raises(request_mod.RequestError, match="legacy route"):
        request_mod.normalize_request([command, "--filter-local-ids", "local-1"], mode_mod)


def test_removed_command_surfaces_stay_removed() -> None:
    from rebar._cli import _registry

    mode_mod = importlib.import_module("rebar.reconciler.mode")
    request_mod = importlib.import_module("rebar.reconciler.request")

    assert _registry.route_for("reconcile") is None
    with pytest.raises(request_mod.RequestError, match="reconcile-check has been removed"):
        request_mod.normalize_request(["--mode", "reconcile-check"], mode_mod)
