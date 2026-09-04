"""The plan-review recovery cluster is a LEAF module (ticket 1484).

``gate_dispatch.py`` sat at 799 LOC against the 800-LOC hard cap — ONE line of headroom — while
three of the five ``orchestrator.finalize_verdict`` call sites live in it, and story 343b must add
an argument at each. That change was physically unlandable. This pins the split that bought the
headroom back.

ADR 0111 retired the internal-only compatibility shims that kept moved symbols reachable from
their old private homes. This file now asserts the canonical modules remain healthy and that the
old re-export surfaces are gone.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm"
_GATE_DISPATCH = _SRC / "workflow" / "gate_dispatch.py"
_RECOVERY = _SRC / "workflow" / "plan_review_recovery.py"
_ORCHESTRATOR = _SRC / "plan_review" / "orchestrator.py"
_CONTEXT_ASSEMBLY = _SRC / "plan_review" / "context_assembly.py"

# AGENTS.md: never create a file under 100 LOC by splitting. This is the ONLY size bound this
# file asserts: the upper ceiling belongs to `.github/module-size-limit.txt` alone, enforced by
# the CI module-size gate and mirrored in-process by test_module_size_contract.py.
_SPLIT_FLOOR = 100


def _loc(path: pathlib.Path) -> int:
    return len(path.read_text().splitlines())


def test_recovery_module_exists() -> None:
    assert _RECOVERY.is_file(), "src/rebar/llm/workflow/plan_review_recovery.py was not created"


def test_extracted_module_clears_the_anti_fragmentation_floor() -> None:
    """A split that produces a tiny file trades one policy violation for another."""
    loc = _loc(_RECOVERY)
    assert loc >= _SPLIT_FLOOR, (
        f"plan_review_recovery.py is {loc} LOC; splitting must not create a file under "
        f"{_SPLIT_FLOOR} LOC"
    )


def test_moved_names_are_no_longer_reachable_from_gate_dispatch() -> None:
    """Plan-review recovery symbols are owned only by ``workflow.plan_review_recovery``."""
    from rebar.llm.workflow import gate_dispatch

    leaked = [
        name
        for name in (
            "STEP_PRECHECK",
            "STEP_ASSEMBLE",
            "STEP_FINDERS",
            "STEP_VERIFY",
            "STEP_DECIDE",
            "STEP_COACH",
            "GateContractError",
            "_collect_step_ids",
            "_validate_gate_step_ids",
            "_attach_plan_review_metrics",
            "_recover_plan_review_coach_failure",
            "_recover_plan_review_verify_failure",
            "_degraded_plan_review_verdict",
        )
        if hasattr(gate_dispatch, name)
    ]
    assert leaked == [], f"gate_dispatch still re-exports plan-review recovery names: {leaked}"


def test_recovery_module_is_a_leaf_at_import_time() -> None:
    """It must not import gate_dispatch at module scope: gate_dispatch imports IT, so a
    module-level back-import would be a partially-initialized-module cycle. Every rebar import in
    the moved bodies is already lazy (inside the functions), so the module level stays bare."""
    tree = ast.parse(_RECOVERY.read_text())
    module_level = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    offenders = [
        n
        for n in module_level
        if isinstance(n, ast.ImportFrom) and "gate_dispatch" in (n.module or "")
    ]
    assert not offenders, (
        "plan_review_recovery.py imports gate_dispatch at module scope — that closes an import "
        f"cycle (lines {[n.lineno for n in offenders]})"
    )


def test_orchestrator_is_reached_by_attribute_access_not_flattened_imports() -> None:
    """LOAD-BEARING, and easy to 'tidy' away. The recovery functions reach orchestrator via a lazy
    ``from rebar.llm.plan_review import orchestrator`` then ``orchestrator.<name>`` attribute
    access. tests/interfaces/lifecycle/test_plan_review_execution_floor_lifecycle.py monkeypatches
    ``orchestrator.pass3_over_findings`` and then calls the recovery function — flattening to
    ``from ...orchestrator import pass3_over_findings`` would bind the original at import time and
    silently defeat that patch."""
    text = _RECOVERY.read_text()
    tree = ast.parse(text)
    flattened = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").endswith("plan_review.orchestrator")
        and any(a.name != "orchestrator" for a in n.names)
    ]
    assert not flattened, (
        "plan_review_recovery.py flattens orchestrator imports to bare names; keep "
        "`from rebar.llm.plan_review import orchestrator` + attribute access so existing "
        f"monkeypatches still resolve (lines {[n.lineno for n in flattened]})"
    )


# ── seam 2: orchestrator.py -> context_assembly.py ────────────────────────────────────────
# Same ticket, same mechanism, different file. 1484's AC list requires BOTH files under the
# headroom target, because `finalize_verdict` — the function 343b adds a parameter to — is
# DEFINED in orchestrator.py, which had 4 lines of headroom.
def test_context_assembly_module_exists() -> None:
    assert _CONTEXT_ASSEMBLY.is_file(), (
        "src/rebar/llm/plan_review/context_assembly.py was not created"
    )


def test_context_assembly_clears_the_anti_fragmentation_floor() -> None:
    loc = _loc(_CONTEXT_ASSEMBLY)
    assert loc >= _SPLIT_FLOOR, (
        f"context_assembly.py is {loc} LOC; splitting must not create a file under "
        f"{_SPLIT_FLOOR} LOC"
    )


def test_moved_assembly_names_are_no_longer_reachable_from_orchestrator() -> None:
    """Context assembly and sizing helpers are no longer re-exported from orchestrator."""
    from rebar.llm.plan_review import orchestrator

    leaked = [
        name
        for name in (
            "assemble_context",
            "assemble_context_cache",
            "_assemble_cache",
            "_assemble_cache_key",
            "_assemble_context_uncached",
            "largest_window_tokens",
        )
        if hasattr(orchestrator, name)
    ]
    assert leaked == [], f"orchestrator still re-exports moved names: {leaked}"


def test_context_assembly_is_a_leaf_at_import_time() -> None:
    """It must not import orchestrator at module scope: orchestrator imports IT."""
    tree = ast.parse(_CONTEXT_ASSEMBLY.read_text())
    offenders = [
        n for n in tree.body if isinstance(n, ast.ImportFrom) and "orchestrator" in (n.module or "")
    ]
    assert not offenders, (
        "context_assembly.py imports orchestrator at module scope — that closes an import cycle "
        f"(lines {[n.lineno for n in offenders]})"
    )


def test_the_memo_still_collapses_repeated_reads_after_the_move() -> None:
    """The cluster's REASON to exist, asserted behaviourally rather than structurally: inside a
    cache scope the same key must return the SAME PlanContext object and read the store ONCE. A
    relocation that dropped the ContextVar (or created a second copy of it under the new module)
    would leave every structural test above green while silently restoring the N+1 read."""
    from rebar.llm.plan_review import context_assembly

    calls: list[str] = []

    def _fake_uncached(ticket_id, *, repo_root=None, cfg=None):
        calls.append(ticket_id)
        return object()

    original = context_assembly._assemble_context_uncached
    context_assembly._assemble_context_uncached = _fake_uncached
    try:
        with context_assembly.assemble_context_cache():
            first = context_assembly.assemble_context("t-1")
            second = context_assembly.assemble_context("t-1")
        assert first is second, "the run-scoped memo did not return the cached object"
        assert calls == ["t-1"], f"expected ONE store read inside the scope, got {len(calls)}"
        # outside a scope every call reads fresh (the documented historical behaviour)
        calls.clear()
        context_assembly.assemble_context("t-1")
        context_assembly.assemble_context("t-1")
        assert calls == ["t-1", "t-1"], "outside a cache scope reads must not be memoized"
    finally:
        context_assembly._assemble_context_uncached = original
