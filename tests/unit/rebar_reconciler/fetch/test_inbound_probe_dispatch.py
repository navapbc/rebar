"""RED tests for reconcile.py inbound-probe routing.

Verifies route_inbound_probe maps each ProbeBranch to the correct branch-specific
follow-on (or log-only outcome):

  * ARCHIVED_OR_MOVED → hard_delete  → (inbound, delete, target) follow-on
  * PRESENT_RESOLVED  → trash_restore → no follow-on, one audit-log entry
  * UNREACHABLE       → unreachable   → no follow-on, audit-log entry
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
PROBE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_probe.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    return _load(MUTATION_PATH, "reconcile_mutation")


@pytest.fixture(scope="module")
def probe_mod():
    # Ensure inbound_probe module is loaded under the same name reconcile uses.
    return _load(PROBE_PATH, "inbound_probe")


@pytest.fixture(scope="module")
def reconcile(probe_mod, mut_mod):  # noqa: ARG001 — fixtures pre-load sys.modules entries
    return _load(RECONCILE_PATH, "reconcile_for_test")


def _make_probe_mutation(mut_mod, target: str = "PROJ-1"):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.probe,
        target=target,
        payload={"reason": "absent_partner"},
        provenance={"source": "differ", "local_target": "LOCAL-1"},
    )


def test_hard_delete_emits_inbound_delete(reconcile, probe_mod, mut_mod):
    """ARCHIVED_OR_MOVED (hard_delete) → emit (inbound, delete, target)."""
    mut = _make_probe_mutation(mut_mod, target="PROJ-1")
    result = probe_mod.ProbeResult(
        probe_mod.ProbeBranch.ARCHIVED_OR_MOVED,
        "PROJ-1",
        {"status_code": 404},
    )
    follow_ons = reconcile.route_inbound_probe(mut, result)
    assert follow_ons is not None
    delete_muts = [
        m
        for m in follow_ons
        if m.action == mut_mod.MutationAction.delete
        and m.direction == mut_mod.MutationDirection.inbound
    ]
    assert len(delete_muts) == 1
    assert delete_muts[0].target == "PROJ-1"


def test_trash_restore_emits_no_follow_on_writes_audit_log(reconcile, probe_mod, mut_mod, capsys):
    """PRESENT_RESOLVED (trash_restore) → NO follow-on; one audit-log entry."""
    mut = _make_probe_mutation(mut_mod, target="PROJ-1")
    result = probe_mod.ProbeResult(
        probe_mod.ProbeBranch.PRESENT_RESOLVED,
        "PROJ-1",
        {"status": "Done"},
    )
    follow_ons = reconcile.route_inbound_probe(mut, result)
    assert follow_ons is None or follow_ons == []
    captured = capsys.readouterr()
    out_err = captured.out + captured.err
    assert "trash_restore" in out_err or "PRESENT_RESOLVED" in out_err
    assert "PROJ-1" in out_err


def test_unreachable_no_follow_on(reconcile, probe_mod, mut_mod, capsys):
    """UNREACHABLE → no follow-on; audit-log entry written."""
    mut = _make_probe_mutation(mut_mod, target="PROJ-1")
    result = probe_mod.ProbeResult(
        probe_mod.ProbeBranch.UNREACHABLE,
        "PROJ-1",
        {"error": "timeout"},
    )
    follow_ons = reconcile.route_inbound_probe(mut, result)
    assert follow_ons is None or follow_ons == []
    captured = capsys.readouterr()
    out_err = captured.out + captured.err
    assert "unreachable" in out_err or "UNREACHABLE" in out_err
    assert "PROJ-1" in out_err
