"""`plan_review_decide` and the operator-attested cluster move to `decide_ops.py` (ticket b5fe).

`plan_review/workflow_ops.py` was 794 LOC against the 800 hard cap. The pressure is NOT that the
file accretes ops — all eight registered ops existed within two days of its creation and none has
been added in 35 days — it is that existing bodies grow: 283 -> 794 with `plan_review_decide`
alone taking +149 of it, 27% of the file and 2.05x the next-largest op.

WHAT THIS CUT IS, and its honest limit. `decide` travels with the operator-attested enrichment
cluster because that cluster has exactly ONE caller in `src/` — `decide` itself — and is documented
as running BEFORE Pass-3 reads the verifications, so it is a Pass-3 pre-step by construction. This
RELOCATES the absorber rather than dissolving it: `decide` is still ~213 lines, now in a file with
room. The ADR-0056-correct follow-up is the verb cut inside it — lifting the prerequisite-coverage
normalisation into `prerequisites.py` — which is deliberately NOT bundled here because it is
behaviour-adjacent and needs its own RED-first test.

ADR 0111 removes the old internal-only `workflow_ops.<name>` shim surface. Tests and
monkeypatches must import these helpers from their canonical owner, `decide_ops`, instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_OPS = REPO_ROOT / "src" / "rebar" / "llm" / "plan_review" / "workflow_ops.py"
_DECIDE_OPS = REPO_ROOT / "src" / "rebar" / "llm" / "plan_review" / "decide_ops.py"

# AGENTS.md: never create a file under 100 LOC by splitting. This is the ONLY LOC bound this
# test asserts — the upper ceiling is `.github/module-size-limit.txt`, enforced by the CI
# module-size gate and mirrored in `test_module_size_contract.py` (ADR 0058).
_FLOOR = 100

_MOVED = (
    "plan_review_decide",
    "enrich_operator_attested",
    "operator_attested_ac_texts",
    "_ticket_id",
    "_OPERATOR_ATTESTED_AC_RE",
)


def _loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_the_extracted_module_exists_and_clears_the_split_floor() -> None:
    """The extraction happened and produced a real module rather than a stub."""
    assert _DECIDE_OPS.exists(), "src/rebar/llm/plan_review/decide_ops.py was not created"
    decide_loc = _loc(_DECIDE_OPS)
    assert decide_loc >= _FLOOR, (
        f"decide_ops.py is {decide_loc} LOC; splitting must not create a file under {_FLOOR} LOC"
    )


def test_moved_names_are_no_longer_reachable_from_workflow_ops() -> None:
    """ADR 0111: internal-only compatibility shims are removed after consumer migration."""
    from rebar.llm.plan_review import workflow_ops

    leaked = [n for n in _MOVED if hasattr(workflow_ops, n)]
    assert leaked == [], f"internal shim still reachable as workflow_ops.<name>: {leaked}"


def test_the_operator_attested_regex_survives_by_object_identity() -> None:
    """The canonical decide_ops import preserves identity with the det-floor matcher."""
    from rebar.llm.plan_review import decide_ops, det_operator_attested

    assert decide_ops._OPERATOR_ATTESTED_AC_RE is det_operator_attested._OPERATOR_ATTESTED_TAG_RE


# ══ HELD OUT ══════════════════════════════════════════════════════════════════════════


def test_decide_ops_is_a_leaf_at_import_time() -> None:
    """`workflow_ops` imports `decide_ops` at module scope for registration, so the reverse would
    close an import cycle — an import-time failure, not a lint nit."""
    tree = ast.parse(_DECIDE_OPS.read_text(encoding="utf-8"))
    bad = [
        f"line {n.lineno}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and "workflow_ops" in (n.module or "")
    ]
    assert bad == [], f"decide_ops imports workflow_ops at module scope: {bad}"


def test_decide_ops_reaches_orchestrator_by_attribute_access() -> None:
    """THE SILENT-BREAK GUARD. `test_plan_review_execution_floor_lifecycle.py` monkeypatches
    `orchestrator.pass3_over_findings` and then calls the decide op. That works only while the op
    resolves the attribute off the MODULE at call time. Flattening to
    `from .orchestrator import pass3_over_findings` binds the original function at import time, so
    the patch still applies to the module, the op keeps calling the unpatched original, and the
    lifecycle test PASSES WHILE ASSERTING NOTHING."""
    tree = ast.parse(_DECIDE_OPS.read_text(encoding="utf-8"))
    flattened = [
        f"line {n.lineno}: {', '.join(a.name for a in n.names)}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("orchestrator")
    ]
    assert flattened == [], (
        "decide_ops flattens orchestrator imports; use `from . import orchestrator` plus attribute "
        f"access so monkeypatching keeps working: {flattened}"
    )


def test_the_registration_import_lives_in_workflow_ops_not_steps() -> None:
    """Registration is an import side effect. `workflow/steps.py` deliberately imports only
    `workflow_ops`, which chains to its own extracted modules — keeping `steps.py` unaware of
    plan-review's internal layout, the precedent `prerequisite_workflow_ops` already set."""
    ops = _WORKFLOW_OPS.read_text(encoding="utf-8")
    steps = (REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "steps.py").read_text(
        encoding="utf-8"
    )
    assert "decide_ops" in ops, "workflow_ops must import decide_ops for registration"
    assert "decide_ops" not in steps, "steps.py must not learn plan-review's internal layout"
