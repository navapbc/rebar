"""Tests for applier.inbound_repair_property — story 7a75-53f5 / task 44e6-4916.

Covers DD-3:
  (inbound, repair_property) failure → applier removes the orphan
  ``rebar-id-<local_id>`` label AND emits a follow-on schema-drift signal in
  the SAME pass; fault-injection asserts both side effects (sc-7).

Import-direction guarantee (F6): applier.py MUST NOT import invariants —
schema-drift is communicated via a 'follow_on' payload that reconcile.py
routes to invariants in the next iteration.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _mutation(target: str = "DIG-42", local_id: str = "abc-123"):
    """Build a minimal mutation stub exposing .target and .payload."""
    return types.SimpleNamespace(
        target=target,
        payload={"local_id": local_id},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path(applier):
    """set_issue_property called once; no label removal; no schema-drift signal."""
    client = MagicMock()
    mutation = _mutation()

    result = applier.inbound_repair_property(mutation, client)

    assert result["status"] == "ok"
    assert result["key"] == "DIG-42"
    # No schema-drift signal on success
    assert result.get("follow_on") is None
    # set_issue_property called exactly once with expected args
    client.set_issue_property.assert_called_once_with("DIG-42", "local_id", "abc-123")
    # No label removal on success
    client.remove_label.assert_not_called()


# ---------------------------------------------------------------------------
# Failure: side effects
# ---------------------------------------------------------------------------


def test_failure_cleans_label_and_signals_drift(applier):
    """set_issue_property raises → remove_label called AND follow_on signal emitted."""
    client = MagicMock()
    client.set_issue_property.side_effect = RuntimeError("simulated property write failure")
    mutation = _mutation(target="DIG-99", local_id="local-99")

    result = applier.inbound_repair_property(mutation, client)

    # Outcome dict shape
    assert result["status"] == "repair_property_failed"
    assert result["key"] == "DIG-99"

    # Label cleanup attempted exactly once with the correct format
    client.remove_label.assert_called_once_with("DIG-99", "rebar-id-local-99")

    # Follow-on schema-drift signal present at top level
    follow_on = result["follow_on"]
    assert follow_on is not None
    assert follow_on["kind"] == "schema_drift_signal"
    assert follow_on["issue_key"] == "DIG-99"
    assert "repair_property_failed" in follow_on["reason"]
    assert "simulated property write failure" in follow_on["reason"]
    # label_remove succeeded → no error recorded
    assert follow_on["label_remove_error"] is None


# ---------------------------------------------------------------------------
# Failure: resilience when remove_label itself raises
# ---------------------------------------------------------------------------


def test_failure_resilient_to_label_remove_error(applier):
    """remove_label raising must NOT prevent the follow-on schema-drift signal."""
    client = MagicMock()
    client.set_issue_property.side_effect = RuntimeError("primary failure")
    client.remove_label.side_effect = RuntimeError("label removal failure")
    mutation = _mutation(target="DIG-7", local_id="loc-7")

    # Must not raise — the function must swallow remove_label errors
    result = applier.inbound_repair_property(mutation, client)

    assert result["status"] == "repair_property_failed"
    assert result["key"] == "DIG-7"

    # remove_label was still attempted
    client.remove_label.assert_called_once_with("DIG-7", "rebar-id-loc-7")

    # Follow-on signal still emitted, with the label_remove_error captured
    follow_on = result["follow_on"]
    assert follow_on is not None
    assert follow_on["kind"] == "schema_drift_signal"
    assert follow_on["issue_key"] == "DIG-7"
    assert "label removal failure" in (follow_on["label_remove_error"] or "")


# ---------------------------------------------------------------------------
# Import-direction guarantee (F6): the applier must not import invariants
# ---------------------------------------------------------------------------
#
# NON-VACUITY (bug 8a5e, same rot class as bug 34c2). This guard used to read exactly one
# file, `applier.py`, and match three literal line prefixes (`from .invariants`,
# `from invariants`, `import invariants`). Both halves went hollow:
#
#   * WRONG FILE. `applier.py` is now a re-export facade; the function the acceptance
#     criterion is actually about, `inbound_repair_property`, lives in `apply_inbound.py`
#     (which carries the AC text verbatim in its own docstring). A `from
#     rebar_reconciler.invariants import ...` added to any implementation module was
#     invisible to the guard.
#   * WRONG SPELLING. The facade imports its siblings as
#     `from rebar_reconciler.<module> import ...`, so even an invariants import placed in
#     `applier.py` itself would not have matched any of the three prefixes.
#
# The repair is the one proven on bug 34c2: derive the scan POPULATION instead of pinning
# it, then assert the population covers the code the contract is about. The population here
# is the facade plus the transitive closure of its intra-package imports — a relocation
# cannot orphan it, because any module the facade's behaviour moves into must be imported
# back for `applier.<name>` to keep resolving.

_RECONCILER_DIR = APPLIER_PATH.parent


def _imported_module_paths(tree: ast.AST) -> set[str]:
    """Every dotted module path named by an `import` / `from ... import` in `tree`.

    `from X import Y` contributes both `X` and `X.Y`, because a module may be imported
    either as the `from` target or as a name off its package.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                modules.add(base)
            modules.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return modules


def _guard_scanned_sources() -> dict[str, str]:
    """The exact source this guard inspects, as ``{module name: source}``.

    Named and separated so the guard's POPULATION is itself assertable — a structural guard
    is only as good as what it is aimed at, and one aimed at a file that no longer holds the
    policed construct passes unconditionally while policing nothing.

    Starts at the `applier.py` facade and walks its intra-package imports transitively, so
    every module the applier's behaviour was split into is scanned too.
    """
    sources: dict[str, str] = {}
    queue = ["applier"]
    while queue:
        name = queue.pop()
        if name in sources:
            continue
        path = _RECONCILER_DIR / f"{name}.py"
        if not path.is_file():
            continue
        sources[name] = path.read_text()
        for module in _imported_module_paths(ast.parse(sources[name])):
            tail = module.rsplit(".", 1)[-1]
            # Only follow siblings inside rebar_reconciler, and never follow invariants
            # itself — its own imports are not the applier's import direction.
            is_sibling = (
                module.startswith(("rebar_reconciler.", "."))
                or (_RECONCILER_DIR / f"{tail}.py").is_file()
            )
            if is_sibling and tail != "invariants":
                queue.append(tail)
    return sources


def _invariants_imports(sources: dict[str, str]) -> list[str]:
    """Every import of the `invariants` module across `sources`, as ``"<module>:<line>"``.

    An AST scan rather than a line-prefix match: the prefixes only ever caught relative and
    bare spellings, while the applier family imports absolutely
    (`from rebar_reconciler.invariants import ...`). Scanning the parsed tree also means a
    comment or docstring naming `invariants` stays legal — several of these modules discuss
    the contract in prose.
    """
    offenders: list[str] = []
    for name, src in sorted(sources.items()):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                named = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                named = [base] + [f"{base}.{a.name}" if base else a.name for a in node.names]
            else:
                continue
            if any(m.rsplit(".", 1)[-1] == "invariants" for m in named if m):
                offenders.append(f"{name}:{node.lineno}")
    return offenders


def test_applier_does_not_import_invariants():
    """The applier must not import invariants — schema drift is communicated via the
    ``follow_on`` payload that reconcile.py routes to invariants on the NEXT iteration.
    Importing it here would collapse the two phases into one and re-enter invariants
    mid-apply."""
    offenders = _invariants_imports(_guard_scanned_sources())
    assert not offenders, (
        f"the applier imports invariants at {offenders} — schema drift must travel as a "
        f"'follow_on' payload for reconcile.py to route, not as a direct call into the "
        f"upstream phase"
    )


def test_the_guard_scans_the_module_that_holds_the_repair_leaf():
    """ANTI-VACUITY (bug 8a5e). The guard above can only fail if the source it scans
    actually holds the code the contract governs. Pinning it to `applier.py` alone is what
    hollowed it out: the facade split moved `inbound_repair_property` into `apply_inbound.py`
    without editing this test, so the guard read a file with no leaf in it.

    Assert the POPULATION, not just the verdict — the module defining the repair leaf must
    be among the sources scanned, so the next relocation fails the build instead of silently
    disarming the guard.
    """
    sources = _guard_scanned_sources()
    holders = sorted(
        name
        for name, src in sources.items()
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "inbound_repair_property"
            for node in ast.walk(ast.parse(src))
        )
    )
    assert holders, (
        f"no module reachable from applier.py defines inbound_repair_property; the guard "
        f"scans {sorted(sources)} and would pass no matter what any of them imported. "
        f"Either the leaf moved out of the applier's import graph (re-aim "
        f"_guard_scanned_sources) or this guard is now unnecessary."
    )
    assert len(sources) > 1, (
        f"the guard scans only {sorted(sources)} — the applier is a facade over sibling "
        f"modules, so a single-module population means the import walk broke"
    )


_SYNTHETIC_OFFENDER_SRC = '''
"""A module that mentions invariants in prose — which stays legal."""
from rebar_reconciler.invariants import check_schema
from rebar_reconciler.differ import diff


def go():
    return check_schema, diff
'''


def test_the_structural_guard_fires_on_a_synthetic_invariants_import():
    """TEETH. The predicate must report the absolute-spelling import on line 3 and leave the
    sibling `differ` import alone. The absolute spelling is the negative control: the
    superseded line-prefix matcher accepted it silently, which is half of why this guard
    could never fail."""
    offenders = _invariants_imports({"synthetic": _SYNTHETIC_OFFENDER_SRC})
    assert offenders == ["synthetic:3"], (
        f"the predicate must report exactly the invariants import on line 3 of the synthetic "
        f"source (and not the differ import on line 4); it reported {offenders!r}"
    )


def test_the_guard_would_catch_an_invariants_import_outside_the_facade():
    """TEETH for the WIDENED scan, driven through the REAL modules rather than a synthetic
    string. A synthetic-source teeth test proves the predicate works but cannot detect the
    guard being aimed at the wrong file — which is exactly how this one survived a facade
    split. Plant the offending import in the module that holds the repair leaf, and require
    the guard to attribute an offender to a module OTHER than the `applier` facade.
    """
    sources = _guard_scanned_sources()
    leaf = next(
        name
        for name, src in sources.items()
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "inbound_repair_property"
            for node in ast.walk(ast.parse(src))
        )
    )
    assert leaf != "applier", "precondition: the repair leaf lives outside the facade"

    mutated = dict(sources)
    mutated[leaf] = "from rebar_reconciler.invariants import check_schema\n" + mutated[leaf]
    offenders = _invariants_imports(mutated)
    assert any(o.startswith(f"{leaf}:") for o in offenders), (
        f"an invariants import planted in {leaf}.py went unreported (offenders: "
        f"{offenders!r}) — the guard is still effectively aimed at applier.py alone, so the "
        f"module that actually holds the repair leaf is unpoliced"
    )
