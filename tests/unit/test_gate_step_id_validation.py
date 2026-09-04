"""Every gate validates its own step ids, not just plan-review (mirror F13).

Ticket 0d3c-ff25-5317-4d73 (misandrous-defaceable-zenaida).

``_validate_gate_step_ids`` existed with ONE call site — plan-review — while the
code-review and completion-verification gates loaded their YAML and checked nothing,
despite each having python that looks its step ids up BY NAME. The failure a rename
produces is silent in both:

* code-review — ``code_review/finalize.py``'s lookups return ``None`` and the
  finalization cluster degrades a recoverable run.
* completion-verification — the row falls through ``completion_metrics``' mapping into
  the "unclassified" timing bucket, so NOTHING errors and the metric is merely wrong.

The point of these tests is not that a call exists; it is that each gate's required set
is DERIVED from the constants its own code references. A set re-typed as string literals
would satisfy a "does it validate?" test while drifting from the thing it guards — which
is the very defect class this epic removes.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from rebar.llm.code_review.finalize import (
    _STEP_DECIDE,
    _STEP_VERIFY,
    CODE_REVIEW_STEP_IDS,
    STEP_ASSEMBLE_DIFF,
)
from rebar.llm.workflow.completion_metrics import (
    _WORKFLOW_STEP_FIELDS,
    WORKFLOW_STEP_IDS,
)
from rebar.llm.workflow.gate_dispatch import _gate_doc
from rebar.llm.workflow.plan_review_recovery import GateContractError, _validate_gate_step_ids

pytestmark = pytest.mark.unit

GATES = [
    ("code-review", CODE_REVIEW_STEP_IDS),
    ("completion-verification", WORKFLOW_STEP_IDS),
]


def _rename_step(node: Any, old: str, new: str) -> bool:
    """Rename the first step whose ``id`` is ``old``, anywhere in the doc. True if found."""
    if isinstance(node, dict):
        if node.get("id") == old:
            node["id"] = new
            return True
        return any(_rename_step(v, old, new) for v in node.values())
    if isinstance(node, list):
        return any(_rename_step(v, old, new) for v in node)
    return False


# ── the committed YAMLs satisfy their own required sets (AC4) ────────────────


@pytest.mark.parametrize(("gate", "required"), GATES)
def test_the_packaged_gate_yaml_declares_every_required_step_id(gate: str, required: frozenset):
    """If this fails the gate cannot dispatch at all — the guard would fire in production."""
    _validate_gate_step_ids(_gate_doc(gate, None), required, gate_name=gate)


# ── a rename is caught, per gate (AC1, AC2) ──────────────────────────────────


@pytest.mark.parametrize(("gate", "required"), GATES)
def test_renaming_a_required_step_id_raises(gate: str, required: frozenset):
    doc = copy.deepcopy(_gate_doc(gate, None))
    victim = sorted(required)[0]
    assert _rename_step(doc, victim, f"{victim}_renamed"), (
        f"{gate}.yaml has no step id {victim!r} to rename — the required set has drifted "
        "from the YAML and this test can no longer prove anything"
    )
    with pytest.raises(GateContractError) as exc:
        _validate_gate_step_ids(doc, required, gate_name=gate)
    message = str(exc.value)
    assert victim in message, message
    assert gate in message, message


# ── the required sets are DERIVED, not re-typed (AC3) ────────────────────────


def test_code_review_required_set_is_built_from_the_referenced_constants():
    """Re-typing these as literals would let the set drift from the lookups it guards."""
    assert CODE_REVIEW_STEP_IDS == frozenset({_STEP_VERIFY, _STEP_DECIDE, STEP_ASSEMBLE_DIFF})


def test_completion_required_set_is_exactly_the_metrics_mapping_keys():
    assert WORKFLOW_STEP_IDS == frozenset(_WORKFLOW_STEP_FIELDS)


def test_completion_required_set_tracks_code_references_not_yaml_contents():
    """AC5. `decide` exists in completion-verification.yaml but no python looks it up, so it
    is correctly NOT required — proving the set is 'ids the code reads', not 'ids the YAML
    defines'. Requiring the latter would fail the gate on any harmless new step."""
    from rebar.llm.workflow.plan_review_recovery import _collect_step_ids

    present = _collect_step_ids(_gate_doc("completion-verification", None).get("steps"))
    assert "decide" in present, "fixture assumption: the YAML still declares a `decide` step"
    assert "decide" not in WORKFLOW_STEP_IDS


# ── every gate that loads a doc also guards it (the call sites) ──────────────


def test_every_gate_doc_load_is_followed_by_a_step_id_validation():
    """The tests above prove the VALIDATOR works; this proves each dispatch CALLS it.

    Deliberately structural. The behavioral route — drive a real dispatch with a doctored
    doc — needs a git worktree and a diff context before the guard is even reached, and it
    would pin only the gates that exist today. The regression this ticket fixes was a gate
    added WITHOUT a guard, so the invariant worth pinning is the general one: in
    gate_dispatch, every `_gate_doc(...)` result is validated inside the same function.
    """
    import ast
    import inspect

    from rebar.llm.workflow import gate_dispatch

    tree = ast.parse(inspect.getsource(gate_dispatch))
    unguarded: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        loads = [
            n.args[0].value
            for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "_gate_doc"
            and n.args
            and isinstance(n.args[0], ast.Constant)
        ]
        if not loads:
            continue
        guarded = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_validate_gate_step_ids"
            for n in ast.walk(func)
        )
        if not guarded:
            unguarded.extend(f"{func.name}() loads {gate!r}" for gate in loads)
    assert not unguarded, (
        "a gate workflow is loaded and dispatched without validating its step ids — a "
        f"rename in that YAML will degrade silently: {unguarded}"
    )
