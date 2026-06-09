"""Tests for health.record_pass() wiring in reconcile_once().

Verifies that after mutations are applied, reconcile_once() calls
health.record_pass() with the correct pass_id and local_mutation_count.
The pre_fsck / post_fsck / per_type_counts placeholders (0 / 0 / {}) are
accepted as correct until task aa2b wires capture_baseline().
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def reconcile_mod():
    """Load a fresh copy of reconcile.py for each test."""
    # Remove any cached copy so we get a clean module with fresh _load() calls.
    sys.modules.pop("reconcile", None)
    if not RECONCILE_PATH.exists():
        pytest.fail(
            f"reconcile.py not found at {RECONCILE_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_module("reconcile", RECONCILE_PATH)


# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------


def _make_stub_fetcher(tmp_path: Path, pass_id: str, snapshot: dict | None = None) -> types.ModuleType:
    """Return a stub fetcher whose fetch_snapshot() writes a JSON file and returns its path."""
    import json

    snapshot = snapshot or {"DIG-1": {"key": "DIG-1", "fields": {"summary": "Test issue"}}}
    snap_dir = tmp_path / "bridge_state" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"{pass_id}.curr.json"
    snap_file.write_text(json.dumps(snapshot))

    mod = types.ModuleType("reconcile_fetcher")
    mod.fetch_snapshot = MagicMock(return_value=snap_file)
    return mod


def _make_stub_differ(mutations: list | None = None) -> types.ModuleType:
    """Return a stub differ whose compute_mutations() returns a fixed list."""
    mutations = mutations if mutations is not None else [{"op": "create", "key": "DIG-1"}]
    mod = types.ModuleType("reconcile_differ")
    mod.compute_mutations = MagicMock(return_value=mutations)
    return mod


def _make_stub_applier(tmp_path: Path, pass_id: str) -> types.ModuleType:
    """Return a stub applier whose apply() writes a manifest file and returns its path."""
    manifest_dir = tmp_path / "bridge_state" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{pass_id}.json"
    manifest_path.write_text("{}")

    mod = types.ModuleType("reconcile_applier")
    mod.apply = MagicMock(return_value=manifest_path)
    return mod


def _make_stub_health() -> types.ModuleType:
    """Return a stub health module whose record_pass() / count_open_by_type() are MagicMocks.

    count_open_by_type() defaults to returning an empty dict; reconcile_once() calls it
    BEFORE record_pass() to compute the per_type_counts payload, so any stub used in
    these tests must expose both attributes.
    """
    mod = types.ModuleType("reconcile_health")
    mod.record_pass = MagicMock(return_value=None)
    mod.count_open_by_type = MagicMock(return_value={})
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health_record_pass_called_after_apply(tmp_path, reconcile_mod):
    """reconcile_once() calls health.record_pass() with correct pass_id and mutation count."""
    pass_id = "test-pass"
    mutations = [{"op": "create", "key": "DIG-1"}, {"op": "update", "key": "DIG-2"}]

    stub_fetcher = _make_stub_fetcher(tmp_path, pass_id)
    stub_differ = _make_stub_differ(mutations)
    stub_applier = _make_stub_applier(tmp_path, pass_id)
    stub_health = _make_stub_health()

    # Pre-register stubs so reconcile._load() picks them up from sys.modules.
    sys.modules["reconcile_fetcher"] = stub_fetcher
    sys.modules["reconcile_differ"] = stub_differ
    sys.modules["reconcile_applier"] = stub_applier
    sys.modules["reconcile_health"] = stub_health

    try:
        result = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        for key in ("reconcile_fetcher", "reconcile_differ", "reconcile_applier", "reconcile_health"):
            sys.modules.pop(key, None)

    stub_health.record_pass.assert_called_once()
    call_kwargs = stub_health.record_pass.call_args

    # Accept both positional and keyword invocations.
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    args = call_kwargs.args if call_kwargs.args else ()

    # Extract pass_id from args[0] or kwargs
    actual_pass_id = kwargs.get("pass_id", args[0] if args else None)
    assert actual_pass_id == pass_id, (
        f"health.record_pass() must be called with pass_id={pass_id!r}, "
        f"got {actual_pass_id!r}"
    )

    # Extract local_mutation_count
    actual_count = kwargs.get("local_mutation_count")
    if actual_count is None and len(args) >= 5:
        actual_count = args[4]
    assert actual_count == len(mutations), (
        f"health.record_pass() must be called with local_mutation_count={len(mutations)}, "
        f"got {actual_count}"
    )

    # Verify the overall result is still correct
    assert result["pass_id"] == pass_id
    assert result["mutation_count"] == len(mutations)


def test_health_record_pass_called_with_zero_mutations(tmp_path, reconcile_mod):
    """reconcile_once() calls health.record_pass() with local_mutation_count=0 when no mutations."""
    pass_id = "zero-mutation-pass"
    mutations: list = []

    stub_fetcher = _make_stub_fetcher(tmp_path, pass_id)
    stub_differ = _make_stub_differ(mutations)
    stub_applier = _make_stub_applier(tmp_path, pass_id)
    stub_health = _make_stub_health()

    sys.modules["reconcile_fetcher"] = stub_fetcher
    sys.modules["reconcile_differ"] = stub_differ
    sys.modules["reconcile_applier"] = stub_applier
    sys.modules["reconcile_health"] = stub_health

    try:
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        for key in ("reconcile_fetcher", "reconcile_differ", "reconcile_applier", "reconcile_health"):
            sys.modules.pop(key, None)

    stub_health.record_pass.assert_called_once()
    call_kwargs = stub_health.record_pass.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    actual_count = kwargs.get("local_mutation_count", None)
    assert actual_count == 0, (
        f"health.record_pass() must be called with local_mutation_count=0 "
        f"when there are no mutations, got {actual_count}"
    )


def test_health_record_pass_still_fires_on_apply_failure(tmp_path, reconcile_mod):
    """F8 regression: reconcile_once must call health.record_pass even when
    applier.apply raises, so failed passes are not invisible to monitoring.

    Before F8, the apply call was unguarded — any exception propagated past
    record_pass, and the failed pass left no health record on disk. The fix
    wraps apply in try/except/finally and emits a degraded record with
    local_mutation_count=0 and an indicative failure_kind.
    """
    pass_id = "apply-failure-pass"
    mutations = [{"op": "create", "key": "DIG-1"}]

    stub_fetcher = _make_stub_fetcher(tmp_path, pass_id)
    stub_differ = _make_stub_differ(mutations)

    # Applier whose apply() raises a generic RuntimeError
    stub_applier = types.ModuleType("reconcile_applier")
    stub_applier.apply = MagicMock(side_effect=RuntimeError("boom"))

    stub_health = _make_stub_health()

    sys.modules["reconcile_fetcher"] = stub_fetcher
    sys.modules["reconcile_differ"] = stub_differ
    sys.modules["reconcile_applier"] = stub_applier
    sys.modules["reconcile_health"] = stub_health

    try:
        # The original apply() error must propagate after health is recorded
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="boom"):
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        for key in ("reconcile_fetcher", "reconcile_differ", "reconcile_applier", "reconcile_health"):
            sys.modules.pop(key, None)

    # health.record_pass MUST have been called even though apply raised
    stub_health.record_pass.assert_called_once()
    kwargs = stub_health.record_pass.call_args.kwargs

    assert kwargs.get("pass_id") == pass_id, (
        f"degraded health record must carry the failing pass_id; got {kwargs!r}"
    )
    assert kwargs.get("local_mutation_count") == 0, (
        "degraded health record must report 0 mutations (apply did not complete)"
    )
    # failure_kind must be present so monitoring can distinguish degraded
    # passes from successful zero-mutation passes.
    assert kwargs.get("failure_kind") is not None, (
        f"degraded health record must include failure_kind; got {kwargs!r}"
    )
    assert kwargs.get("failure_kind") in ("apply_error", "reschedule"), (
        f"failure_kind must be one of the documented values; got "
        f"{kwargs.get('failure_kind')!r}"
    )


def test_health_per_type_counts_uses_count_open_by_type_result(tmp_path, reconcile_mod):
    """reconcile_once() passes the result of health.count_open_by_type() through
    to health.record_pass() as the per_type_counts kwarg.

    pre_fsck / post_fsck remain placeholder zeros until capture_baseline() wiring
    lands; per_type_counts is now sourced from the live count_open_by_type() call
    rather than a hard-coded {} placeholder.
    """
    pass_id = "placeholder-pass"
    mutations = [{"op": "create", "key": "DIG-99"}]

    stub_fetcher = _make_stub_fetcher(tmp_path, pass_id)
    stub_differ = _make_stub_differ(mutations)
    stub_applier = _make_stub_applier(tmp_path, pass_id)
    stub_health = _make_stub_health()
    # Make count_open_by_type return a non-trivial value so we can verify
    # the result actually flows into record_pass.
    expected_counts = {"epic": 2, "story": 5}
    stub_health.count_open_by_type = MagicMock(return_value=expected_counts)

    sys.modules["reconcile_fetcher"] = stub_fetcher
    sys.modules["reconcile_differ"] = stub_differ
    sys.modules["reconcile_applier"] = stub_applier
    sys.modules["reconcile_health"] = stub_health

    try:
        reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
    finally:
        for key in ("reconcile_fetcher", "reconcile_differ", "reconcile_applier", "reconcile_health"):
            sys.modules.pop(key, None)

    stub_health.count_open_by_type.assert_called_once()
    stub_health.record_pass.assert_called_once()
    call_kwargs = stub_health.record_pass.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}

    assert kwargs.get("pre_fsck") == 0, (
        f"pre_fsck placeholder must be 0, got {kwargs.get('pre_fsck')}"
    )
    assert kwargs.get("post_fsck") == 0, (
        f"post_fsck placeholder must be 0, got {kwargs.get('post_fsck')}"
    )
    assert kwargs.get("per_type_counts") == expected_counts, (
        f"per_type_counts must equal count_open_by_type() result {expected_counts!r}, "
        f"got {kwargs.get('per_type_counts')!r}"
    )
