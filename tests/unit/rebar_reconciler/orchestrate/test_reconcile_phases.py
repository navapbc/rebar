"""Direct per-phase unit tests for the decomposed ``reconcile_once`` (hush-quail-holm).

Before the decomposition, the load / persist logic could only be exercised by
driving the whole ``reconcile_once`` orchestrator (a full fetch → diff → apply →
persist pass). The phases are now in-file helpers over a ``_PassContext``, so this
module exercises them DIRECTLY in isolation:

  - ``_handle_corrupt_snapshot`` — the load-phase corrupt-snapshot abort, lifted
    out of the spine.
  - ``_persist_and_log`` — the persist phase: the manifest tally, the no-write
    plan surfacing, and the filter metadata, each without any fetch/diff/apply.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reconcile_mod():
    assert RECONCILE_PATH.is_file(), f"reconcile.py not found at {RECONCILE_PATH}"
    return _load_module("reconcile_phases_under_test", RECONCILE_PATH)


# --- load phase: the corrupt-snapshot abort (lifted helper) --------------------


def test_handle_corrupt_snapshot_always_raises(reconcile_mod, tmp_path: Path) -> None:
    """A corrupt prev_snapshot must abort the pass — the helper always raises
    RuntimeError (never lets the pass proceed with unknown Jira state), naming the
    offending file, and chains the original parse error as the cause.
    """
    prev_path = tmp_path / "prev_snapshot.json"
    prev_path.write_text("<<<<<<< HEAD\n{} not json\n", encoding="utf-8")
    original = ValueError("Extra data: line 1 column 1")

    with pytest.raises(RuntimeError) as excinfo:
        reconcile_mod._handle_corrupt_snapshot("pass-xyz", tmp_path, prev_path, original)

    assert str(prev_path) in str(excinfo.value), "abort message must name the corrupt file"
    assert excinfo.value.__cause__ is original, "must chain the original parse error as cause"


# --- persist phase: manifest tally / no-write plan / filter metadata -----------


def _ctx(reconcile_mod, tmp_path: Path, **overrides):
    """Build a _PassContext for the persist phase with a no-op sync logger.

    persist defaults to False so the persist phase never touches the binding
    store / snapshot files — isolating the tally + result-assembly logic.
    """
    base = dict(
        pass_id="pass-1",
        repo_root=tmp_path,
        persist=False,
        sync_logger=reconcile_mod._NoOpSyncLogger(),
        mutations=[],
        manifest_path=None,
        nowrite_plan=None,
    )
    base.update(overrides)
    return reconcile_mod._PassContext(**base)


def test_persist_and_log_manifest_tally(reconcile_mod, tmp_path: Path) -> None:
    """The persist phase parses the applier manifest into the TRUTHFUL applied /
    failure counts: an outcome with no ``error`` counts as applied, one with an
    ``error`` counts as a failure (bug 85a1).
    """
    manifest = tmp_path / "pass-1.manifest.json"
    manifest.write_text(
        json.dumps({"mutations": [{"action": "create"}, {"action": "update", "error": "boom"}]}),
        encoding="utf-8",
    )
    ctx = _ctx(reconcile_mod, tmp_path, mutations=[{}, {}], manifest_path=manifest)

    result = reconcile_mod._persist_and_log(ctx)

    assert result["mutation_count"] == 2
    assert result["mutations_applied"] == 1
    assert result["mutation_failures"] == 1
    assert result["manifest_path"] == str(manifest)


def test_persist_and_log_no_write_plan(reconcile_mod, tmp_path: Path) -> None:
    """No-write (cap-0) mode: nothing is applied, so the tally is (0, 0) and the
    result surfaces the computed plan + no_write/mode keys.
    """
    ctx = _ctx(reconcile_mod, tmp_path, mutations=[], nowrite_plan={"applied_count": 0})

    result = reconcile_mod._persist_and_log(ctx)

    assert result["no_write"] is True
    assert result["mutations_applied"] == 0
    assert result["mutation_failures"] == 0
    assert "plan" in result and result["plan"] == []


def test_persist_and_log_filter_metadata(reconcile_mod, tmp_path: Path) -> None:
    """A filtered pass surfaces the filter metadata (sorted ids + the pre-filter
    count) in the result dict.
    """
    ctx = _ctx(
        reconcile_mod,
        tmp_path,
        mutations=[{}],
        filter_local_ids={"b-2", "a-1"},
        unfiltered_count=7,
    )

    result = reconcile_mod._persist_and_log(ctx)

    assert result["filtered"] is True
    assert result["filter_local_ids"] == ["a-1", "b-2"]
    assert result["unfiltered_mutation_count"] == 7
