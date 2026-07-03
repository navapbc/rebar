"""Tests for the import-cycle strangler guard (scripts/check_import_cycles.py).

The guard freezes the current internal import-cycle surface of ``src/rebar`` and
fails CI when a change makes it worse (a bigger largest-SCC, a new cross-package
cycle, or a forbidden "reach up" layering import). These tests prove BOTH halves:

  * the current tree PASSES the committed baseline and carries no forbidden edge; and
  * a NEWLY-introduced cross-package cycle / SCC growth / forbidden import is CAUGHT.

The "caught" cases run against small synthetic fixture graphs, so no real forbidden
edge is ever added to the tree.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_import_cycles.py"
_BASELINE = _REPO_ROOT / ".import-cycle-baseline.json"


def _load_guard():
    """Import the standalone guard script as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("check_import_cycles", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# ── Pure-algorithm tests (no grimp, no real tree) ──────────────────────────────


def test_tarjan_identifies_strongly_connected_components() -> None:
    # {a,b,c} form a cycle; d only feeds into it; e is isolated.
    adjacency = {
        "a": ["b"],
        "b": ["c"],
        "c": ["a"],
        "d": ["c"],
        "e": [],
    }
    cyclic = [sorted(c) for c in guard.strongly_connected_components(adjacency) if len(c) > 1]
    assert cyclic == [["a", "b", "c"]]


def test_top_package_granularity() -> None:
    assert guard.top_package("rebar.llm.workflow.foo") == "llm"
    assert guard.top_package("rebar.signing") == "signing"
    assert guard.top_package("rebar") == "rebar"


def test_analyze_reports_cross_package_pairs() -> None:
    # A 3-module cycle spanning two top-level packages (pkga, pkgb).
    adjacency = {
        "rebar.pkga.one": ["rebar.pkgb.two"],
        "rebar.pkgb.two": ["rebar.pkga.three"],
        "rebar.pkga.three": ["rebar.pkga.one"],
        "rebar.leaf": [],
    }
    metrics = guard.analyze(adjacency)
    assert metrics["largest_scc_size"] == 3
    assert metrics["cross_package_cycle_pairs"] == ["pkga::pkgb"]


# ── Regression-detection tests: a NEW cross-package cycle must FAIL ─────────────


def test_new_cross_package_cycle_is_detected() -> None:
    """The core AC: a synthetic new cross-package cycle is flagged as a regression."""
    # Baseline: no cycles at all.
    baseline = {"largest_scc_size": 0, "cross_package_cycle_pairs": []}
    # Current tree grows a cycle between two previously-clean packages.
    adjacency = {
        "rebar.graph.a": ["rebar.reducer.b"],
        "rebar.reducer.b": ["rebar.graph.a"],
    }
    current = guard.analyze(adjacency)
    regressions, _ = guard.compare(current, baseline)
    assert regressions, "a new cross-package cycle must be reported as a regression"
    assert any("graph::reducer" in r for r in regressions)


def test_growing_largest_scc_is_detected() -> None:
    baseline = {"largest_scc_size": 3, "cross_package_cycle_pairs": ["pkga::pkgb"]}
    # A 4-module cycle within the already-co-cyclic pair: no new pair, but larger SCC.
    adjacency = {
        "rebar.pkga.one": ["rebar.pkgb.two"],
        "rebar.pkgb.two": ["rebar.pkga.three"],
        "rebar.pkga.three": ["rebar.pkgb.four"],
        "rebar.pkgb.four": ["rebar.pkga.one"],
    }
    current = guard.analyze(adjacency)
    regressions, _ = guard.compare(current, baseline)
    assert regressions
    assert any("grew" in r for r in regressions)


def test_no_regression_when_within_baseline() -> None:
    baseline = {"largest_scc_size": 3, "cross_package_cycle_pairs": ["pkga::pkgb"]}
    adjacency = {
        "rebar.pkga.one": ["rebar.pkgb.two"],
        "rebar.pkgb.two": ["rebar.pkga.one"],
    }
    current = guard.analyze(adjacency)
    regressions, improvements = guard.compare(current, baseline)
    assert regressions == []
    # The 3->2 shrink and the pair still present -> flagged as an improvement, not a fail.
    assert any("shrank" in i for i in improvements)


def test_forbidden_layer_import_is_detected() -> None:
    """A synthetic reducer -> llm edge (a "reach up") is a forbidden-layer violation."""
    adjacency = {
        "rebar.reducer._processors": ["rebar.llm.review", "rebar._ids"],
        "rebar._ids": [],
    }
    violations = guard.forbidden_layer_violations(adjacency)
    assert ("rebar.reducer._processors", "rebar.llm.review") in violations
    # The allowed leaf import (rebar._ids) is NOT flagged.
    assert all(imported != "rebar._ids" for _, imported in violations)


# ── Real-tree tests (require grimp; installed via the [dev] extra) ──────────────

grimp = pytest.importorskip("grimp", reason="grimp is a [dev] dependency of the guard")


def test_committed_baseline_matches_current_tree() -> None:
    """The guard PASSES the current tree, and the committed baseline is honest.

    Exact-match (not merely "no regression") so a stale/loosened baseline is caught:
    if the graph improves, this fails with a pointer to `--update`, keeping the
    ratchet tight.
    """
    adjacency = guard.build_adjacency()
    current = guard.analyze(adjacency)
    baseline = json.loads(_BASELINE.read_text())
    assert current["largest_scc_size"] == baseline["largest_scc_size"]
    assert current["cross_package_cycle_pairs"] == baseline["cross_package_cycle_pairs"]
    assert current["total_modules_in_cycles"] == baseline["total_modules_in_cycles"]
    # And the guard's own compare() sees no regression.
    regressions, _ = guard.compare(current, baseline)
    assert regressions == []


def test_real_tree_has_no_forbidden_layer_imports() -> None:
    """AC: no real forbidden edge is left in the tree (reducer stays a leaf)."""
    adjacency = guard.build_adjacency()
    assert guard.forbidden_layer_violations(adjacency) == []
