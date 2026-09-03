"""RP-06 S7 — the cross-gate boundary guard (AC3).

A repository-policy / contract test that FAILS the build on the three ways the cross-gate
cutover could be silently undone:

  (a) a review gate reading packaged/project routing DIRECTLY (``overlay._load_overlay`` or
      the ``criteria_routing.json`` resource), bypassing the single ``CriteriaSnapshot``
      policy authority;
  (b) a SECOND discovery scheduler outside the shared kernel — a module that re-implements
      the dependency-ordered unit executor (a ``graphlib`` topological schedule over the
      kernel's ``DiscoveryUnitPlan``/``DiscoveryStagePlan`` types) instead of calling the one
      ``review_kernel.discovery.execute_stage``;
  (c) a per-unit trace/debug field added to the review or review-status public OUTPUT
      schemas (which must stay narrow — internal traces live in the reducer-ignored journal).

Each detector is proven to have TEETH against a synthetic violation, then asserted clean on
the real tree (the ``repo_policy`` cases). The detectors are semantic/AST — a docstring or
comment MENTIONING a prohibited symbol is never a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

from rebar import schemas

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "rebar"
_LLM = _SRC / "llm"
_KERNEL = _LLM / "review_kernel"

# The review gates that MUST each still own a discovery-projection seam — the place where
# that gate's review policy becomes kernel discovery units. The builders themselves are
# DISCOVERED from the tree (see ``_projection_builders``) rather than hand-listed, so a new
# builder is guarded the moment it appears and moving one between modules cannot silently
# drop it out of the guard's scope; this tuple only pins that the seam has not vanished.
_PROJECTION_BUILDER_GATES = ("plan_review", "code_review")

# Per-unit trace/debug keys that must never surface on a public review output schema.
_TRACE_DEBUG_KEYS = frozenset(
    {
        "discovery_trace",
        "discovery_traces",
        "unit_trace",
        "unit_traces",
        "traces",
        "per_unit",
        "per_unit_trace",
        "per_unit_traces",
        "lineage",
        "envelope",
        "envelopes",
        "checkpoint",
        "checkpoints",
        "debug",
    }
)


# ── detectors (semantic / AST) ─────────────────────────────────────────────────
def _reads_raw_routing(tree: ast.AST) -> bool:
    """True iff the module reads RAW effective/project routing directly — a call to
    ``_load_overlay`` (bare or ``overlay._load_overlay``) or a ``criteria_routing.json``
    string literal used as a resource/path — as opposed to consuming a ``CriteriaSnapshot``.
    The sanctioned chain (``registry.effective_routing`` → ``overlay.effective_routing``) is
    NOT flagged."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_load_overlay":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "_load_overlay":
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "criteria_routing.json" in node.value:
                return True
    return False


def _constructs_discovery_stage(tree: ast.AST) -> bool:
    """True iff the module CONSTRUCTS a ``DiscoveryStagePlan`` (i.e. it is a discovery
    projection builder / review-gate discovery seam)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "DiscoveryStagePlan":
                return True
    return False


def _references_kernel_unit_types(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {"DiscoveryStagePlan", "DiscoveryUnitPlan"}:
            return True
        if isinstance(node, ast.Attribute) and node.attr in {
            "DiscoveryStagePlan",
            "DiscoveryUnitPlan",
        }:
            return True
    return False


def _builds_topological_scheduler(tree: ast.AST) -> bool:
    """True iff the module constructs a ``graphlib.TopologicalSorter`` (the kernel's
    dependency-scheduling primitive)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "TopologicalSorter":
                return True
    return False


def _defines_execute_stage(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name == "execute_stage"
        for node in ast.walk(tree)
    )


def _is_direct_reader_violation(source: str) -> bool:
    """AC3(a): a discovery projection builder that ALSO reads raw routing directly."""
    tree = ast.parse(source)
    return _constructs_discovery_stage(tree) and _reads_raw_routing(tree)


def _is_second_scheduler_violation(source: str) -> bool:
    """AC3(b): a module that re-implements the discovery scheduler — a graphlib topological
    schedule over the kernel unit types, outside the kernel."""
    tree = ast.parse(source)
    return _builds_topological_scheduler(tree) and _references_kernel_unit_types(tree)


def _trace_debug_keys_in_output(schema: dict) -> set[str]:
    """AC3(c): the per-unit trace/debug keys declared at the TOP level of a public output."""
    props = schema.get("properties", {})
    return set(props) & _TRACE_DEBUG_KEYS


def _iter_llm_sources() -> list[tuple[Path, ast.AST]]:
    return [(module.path, module.tree) for module in parsed_python_files(_LLM)]


def _projection_builders() -> list[tuple[Path, ast.AST]]:
    """Every module under ``src/rebar/llm`` that CONSTRUCTS a ``DiscoveryStagePlan`` — the
    discovery-projection builders, found by scanning the tree instead of by hand-listing
    paths. Whole-tree discovery is what gives the AC3(a) prohibition its reach: a builder
    added in a new module, or moved to a different one, is still covered."""
    return [(path, tree) for path, tree in _iter_llm_sources() if _constructs_discovery_stage(tree)]


# ── AC3(a): direct-routing-reader prohibition ───────────────────────────────────
def test_direct_routing_reader_detector_has_teeth() -> None:
    violation = (
        "from rebar.llm.criteria import overlay\n"
        "def build(repo):\n"
        "    raw = overlay._load_overlay(repo)\n"
        "    return DiscoveryStagePlan(units=(), material='')\n"
    )
    assert _is_direct_reader_violation(violation) is True


def test_direct_routing_reader_detector_ignores_the_sanctioned_snapshot_chain() -> None:
    clean = (
        "def build(snapshot):\n"
        "    routing = snapshot.routing('plan_review')\n"
        "    return DiscoveryStagePlan(units=(), material='')\n"
    )
    assert _is_direct_reader_violation(clean) is False


def test_a_criteria_routing_json_literal_in_a_builder_is_a_violation() -> None:
    violation = (
        "import json, pathlib\n"
        "def build(repo):\n"
        "    data = json.loads(pathlib.Path(repo, '.rebar/criteria_routing.json').read_text())\n"
        "    return DiscoveryStagePlan(units=(), material='')\n"
    )
    assert _is_direct_reader_violation(violation) is True


@pytest.mark.repo_policy
def test_no_projection_builder_reads_raw_routing_directly() -> None:
    builders = _projection_builders()
    gates = {path.relative_to(_LLM).parts[0] for path, _ in builders}
    for gate in _PROJECTION_BUILDER_GATES:
        assert gate in gates, (
            f"no discovery-projection builder found under llm/{gate}/ — the seam where that "
            "gate's policy becomes kernel discovery units must exist for this guard to bind"
        )
    offenders = [
        str(path.relative_to(_REPO_ROOT)) for path, tree in builders if _reads_raw_routing(tree)
    ]
    assert not offenders, (
        "review-gate discovery builder(s) read raw routing directly instead of via "
        f"CriteriaSnapshot: {offenders}"
    )


# ── AC3(b): single-scheduler prohibition ────────────────────────────────────────
def test_second_scheduler_detector_has_teeth() -> None:
    violation = (
        "import graphlib\n"
        "def run(plan):\n"
        "    units = {u.unit_id: u.dependencies for u in plan.units}\n"
        "    sorter = graphlib.TopologicalSorter(units)\n"
        "    for u in sorter.static_order():\n"
        "        DiscoveryUnitPlan(unit_id=u)\n"
    )
    assert _is_second_scheduler_violation(violation) is True


def test_second_scheduler_detector_ignores_a_plain_kernel_consumer() -> None:
    clean = (
        "from rebar.llm.review_kernel import execute_stage\n"
        "def run(plan, run_unit):\n"
        "    return execute_stage(plan, run_unit, store=None)\n"
    )
    assert _is_second_scheduler_violation(clean) is False


@pytest.mark.repo_policy
def test_execute_stage_is_defined_exactly_once_in_the_kernel() -> None:
    definers = [path for path, tree in _iter_llm_sources() if _defines_execute_stage(tree)]
    assert definers == [_KERNEL / "discovery.py"], (
        f"execute_stage must be the single scheduler in review_kernel/discovery.py; got {definers}"
    )


@pytest.mark.repo_policy
def test_no_second_discovery_scheduler_outside_the_kernel() -> None:
    offenders = []
    for path, tree in _iter_llm_sources():
        if path.is_relative_to(_KERNEL):
            continue
        if _builds_topological_scheduler(tree) and _references_kernel_unit_types(tree):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "a second discovery scheduler exists outside review_kernel/ — route unit scheduling "
        f"through execute_stage instead: {offenders}"
    )


# ── AC3(c): public output schemas carry no per-unit trace/debug field ───────────
def test_schema_trace_key_detector_has_teeth() -> None:
    polluted = {"properties": {"verdict": {}, "discovery_trace": {}, "unit_traces": {}}}
    assert _trace_debug_keys_in_output(polluted) == {"discovery_trace", "unit_traces"}


_PUBLIC_OUTPUT_SCHEMAS = (
    schemas.PLAN_REVIEW_VERDICT,
    schemas.PLAN_REVIEW_STATUS,
    schemas.CODE_REVIEW_VERDICT,
    schemas.REVIEW_RESULT,
)


@pytest.mark.repo_policy
@pytest.mark.parametrize("name", _PUBLIC_OUTPUT_SCHEMAS)
def test_public_output_schema_declares_no_per_unit_trace_field(name: str) -> None:
    leaked = _trace_debug_keys_in_output(schemas.load(name))
    assert not leaked, (
        f"public output schema {name!r} declares per-unit trace/debug key(s) {sorted(leaked)} — "
        "internal discovery traces belong in the reducer-ignored journal, not the verdict"
    )
