"""Tests for the invariant phase wired into reconcile_once().

Covers task 52f3-f71a-dfaa-4d03:
  - reconcile_once() runs invariants.check_dual_identity_complete() and
    passes its outputs to differ.compute_mutations() as quarantine_set= and
    seed_mutations= kwargs.
  - reconcile_once() runs a post-emit filter that invokes
    invariants.report_schema_drift() for every repair_property mutation
    whose payload carries follow_on={'kind':'schema_drift', ...}.
  - The same-pass follow-on signal is preserved end-to-end (returned in
    the same pass, not deferred).

All collaborators (fetcher, differ, applier, health, invariants) are mocked
via sys.modules so reconcile._load() picks them up.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "reconcile.py"
)

_RECONCILE_COLLAB_KEYS = (
    "reconcile_fetcher",
    "reconcile_differ",
    "reconcile_applier",
    "reconcile_health",
    "reconcile_invariants",
)


def _load_reconcile() -> ModuleType:
    sys.modules.pop("reconcile", None)
    spec = importlib.util.spec_from_file_location("reconcile", RECONCILE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_stub_fetcher(snapshot: dict) -> ModuleType:
    stub = types.ModuleType("reconcile_fetcher")

    def _fetch(pid, repo_root):  # noqa: ANN001
        out_dir = repo_root / "bridge_state" / "snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{pid}.json"
        out.write_text(json.dumps(snapshot))
        return out

    stub.fetch_snapshot = _fetch
    return stub


def _make_stub_applier() -> ModuleType:
    stub = types.ModuleType("reconcile_applier")

    def _apply(mutations, pass_id, repo_root, **kwargs):  # noqa: ANN001
        # Bug 85a1: reconcile_once now passes binding_store= (and may add
        # more kwargs in the future); accept and ignore for stub purposes.
        manifest = repo_root / "bridge_state" / "manifests" / f"{pass_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"mutations": len(mutations)}))
        return manifest

    stub.apply = _apply
    return stub


def _make_stub_health() -> ModuleType:
    stub = types.ModuleType("reconcile_health")
    stub.record_pass = lambda **kwargs: None
    stub.count_open_by_type = lambda repo_root=None: {}
    return stub


def _make_stub_differ(returned_mutations=None) -> tuple[ModuleType, MagicMock]:
    """Return (stub_module, spy) where spy records compute_mutations calls."""
    stub = types.ModuleType("reconcile_differ")
    spy = MagicMock(return_value=returned_mutations or [])
    stub.compute_mutations = spy
    return stub, spy


def _make_stub_invariants(
    quarantine: set[str] | None = None,
    seeds: list[dict] | None = None,
) -> tuple[ModuleType, MagicMock]:
    """Return (stub_module, drift_spy) where drift_spy records report_schema_drift calls."""
    stub = types.ModuleType("reconcile_invariants")
    stub.check_at_most_one_dso_local_id = MagicMock(return_value=[])
    stub.check_dual_identity_complete = MagicMock(
        return_value=(quarantine or set(), seeds or [])
    )
    drift_spy = MagicMock(return_value=None)
    stub.report_schema_drift = drift_spy
    return stub, drift_spy


def _install_stubs(*, fetcher, differ, applier, health, invariants) -> None:
    for key in _RECONCILE_COLLAB_KEYS:
        sys.modules.pop(key, None)
    sys.modules["reconcile_fetcher"] = fetcher
    sys.modules["reconcile_differ"] = differ
    sys.modules["reconcile_applier"] = applier
    sys.modules["reconcile_health"] = health
    sys.modules["reconcile_invariants"] = invariants


def _cleanup_stubs() -> None:
    for key in _RECONCILE_COLLAB_KEYS + ("reconcile",):
        sys.modules.pop(key, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_invariant_phase_passes_quarantine_set_to_differ(tmp_path):
    """compute_mutations must be called with quarantine_set kwarg.

    Behavior under test: the invariant phase computes a quarantine set of
    one-sided keys; that set must reach the differ so the differ can skip
    those keys instead of emitting create/delete mutations against them.
    """
    pass_id = "inv-phase-quarantine"
    snapshot = {"JIRA-1": {"summary": "x"}}
    quarantine = {"JIRA-99"}

    stub_fetcher = _make_stub_fetcher(snapshot)
    stub_differ, differ_spy = _make_stub_differ()
    stub_applier = _make_stub_applier()
    stub_health = _make_stub_health()
    stub_invariants, _ = _make_stub_invariants(quarantine=quarantine)

    _install_stubs(
        fetcher=stub_fetcher,
        differ=stub_differ,
        applier=stub_applier,
        health=stub_health,
        invariants=stub_invariants,
    )
    try:
        reconcile_mod = _load_reconcile()
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        _cleanup_stubs()

    assert differ_spy.call_count == 1
    kwargs = differ_spy.call_args.kwargs
    assert "quarantine_set" in kwargs, (
        "compute_mutations must be called with quarantine_set kwarg"
    )
    assert kwargs["quarantine_set"] == quarantine


def test_invariant_phase_passes_seed_mutations_to_differ(tmp_path):
    """compute_mutations must be called with seed_mutations kwarg.

    Behavior under test: the invariant phase produces seed repair_property
    mutations for one-sided dso_local_id rows; those seeds must reach the
    differ so they land in the same pass (no deferral).
    """
    pass_id = "inv-phase-seeds"
    snapshot = {"JIRA-1": {"summary": "x"}}
    seeds = [
        {
            "action": "repair_property",
            "key": "JIRA-7",
            "local_id": "local-7",
            "fields": {"dso_local_id": "local-7"},
        }
    ]

    stub_fetcher = _make_stub_fetcher(snapshot)
    stub_differ, differ_spy = _make_stub_differ()
    stub_applier = _make_stub_applier()
    stub_health = _make_stub_health()
    stub_invariants, _ = _make_stub_invariants(seeds=seeds)

    _install_stubs(
        fetcher=stub_fetcher,
        differ=stub_differ,
        applier=stub_applier,
        health=stub_health,
        invariants=stub_invariants,
    )
    try:
        reconcile_mod = _load_reconcile()
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        _cleanup_stubs()

    assert differ_spy.call_count == 1
    kwargs = differ_spy.call_args.kwargs
    assert "seed_mutations" in kwargs, (
        "compute_mutations must be called with seed_mutations kwarg"
    )
    assert kwargs["seed_mutations"] == seeds


def test_schema_drift_post_emit_filter_invokes_report(tmp_path):
    """report_schema_drift must be invoked for repair_property follow-ons.

    Behavior under test: when the differ returns a repair_property mutation
    whose payload carries follow_on={'kind':'schema_drift', ...}, the
    post-emit filter in reconcile_once must call report_schema_drift with
    the target/observed/expected fields from the follow-on payload.
    """
    pass_id = "inv-phase-drift"
    snapshot = {"JIRA-1": {"summary": "x"}}
    mutations = [
        {
            "action": "repair_property",
            "key": "JIRA-7",
            "local_id": "local-7",
            "fields": {"dso_local_id": "local-7"},
            "follow_on": {
                "kind": "schema_drift",
                "target": "dso_local_id",
                "observed": "STRING",
                "expected": "ARRAY",
            },
        }
    ]

    stub_fetcher = _make_stub_fetcher(snapshot)
    stub_differ, _ = _make_stub_differ(returned_mutations=mutations)
    stub_applier = _make_stub_applier()
    stub_health = _make_stub_health()
    stub_invariants, drift_spy = _make_stub_invariants()

    _install_stubs(
        fetcher=stub_fetcher,
        differ=stub_differ,
        applier=stub_applier,
        health=stub_health,
        invariants=stub_invariants,
    )
    try:
        reconcile_mod = _load_reconcile()
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        _cleanup_stubs()

    assert drift_spy.call_count == 1, (
        f"Expected report_schema_drift to be called once, got {drift_spy.call_count}"
    )
    args = drift_spy.call_args.args
    assert args == ("dso_local_id", "STRING", "ARRAY"), (
        f"report_schema_drift called with unexpected args: {args!r}"
    )


def test_same_pass_follow_on_emitted(tmp_path):
    """schema_drift follow-on must surface in the same pass, not be deferred.

    Behavior under test: a repair_property mutation that carries a
    schema_drift follow-on triggers report_schema_drift WITHIN the same
    reconcile_once() call that produced the mutation — not on a subsequent
    pass. Verified by asserting drift_spy fires before reconcile_once
    returns.
    """
    pass_id = "inv-phase-same-pass"
    snapshot = {"JIRA-1": {"summary": "x"}}
    mutations = [
        {
            "action": "repair_property",
            "key": "JIRA-9",
            "local_id": "local-9",
            "fields": {"dso_local_id": "local-9"},
            "follow_on": {
                "kind": "schema_drift",
                "target": "labels",
                "observed": "STRING",
                "expected": "ARRAY",
            },
        }
    ]

    stub_fetcher = _make_stub_fetcher(snapshot)
    stub_differ, _ = _make_stub_differ(returned_mutations=mutations)
    stub_applier = _make_stub_applier()
    stub_health = _make_stub_health()
    stub_invariants, drift_spy = _make_stub_invariants()

    _install_stubs(
        fetcher=stub_fetcher,
        differ=stub_differ,
        applier=stub_applier,
        health=stub_health,
        invariants=stub_invariants,
    )
    try:
        reconcile_mod = _load_reconcile()
        result = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        _cleanup_stubs()

    # The single reconcile_once() call must have invoked report_schema_drift
    # at least once — i.e. the follow-on signal is processed in-pass, not
    # deferred to a later reconcile_once invocation.
    assert drift_spy.called, (
        "report_schema_drift must fire within the same pass that produced the follow-on"
    )
    assert result["pass_id"] == pass_id
