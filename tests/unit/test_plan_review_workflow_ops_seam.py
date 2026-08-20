"""Seam guards for the plan-review workflow ops (ticket d8ef).

Two guards that did not exist at ANY layer before this file.

WHY THE REGISTRY CHECKS RUN IN A SUBPROCESS — do not "simplify" this away.
`@register_step` populates `STEP_REGISTRY` (a process-global dict, `workflow/executor.py:166`) as an
IMPORT SIDE EFFECT, so ANY module that imports a registering module populates it for the whole
session. The masking path is a SIBLING TEST:
`tests/unit/test_plan_review_prerequisites_heldout.py:11`
imports `prerequisite_workflow_ops` DIRECTLY, which registers
`plan_review_prerequisite_verify_inputs` regardless of whether the production side-effect import
at `plan_review/workflow_ops.py:45` still exists.

MEASURED, with that `:45` import deleted:
  * an in-process assertion run ALONE      -> FAILS (it does detect the missing op);
  * the same assertion run AFTER
    test_plan_review_prerequisites_heldout -> PASSES, 12 passed — fully MASKED;
  * this subprocess form, same conditions  -> FAILS, correctly naming the missing op.

So the hazard is not module caching in the abstract — a source deletion is seen by every import in
a fresh process — it is that a test module's own convenience import silently substitutes for the
production one. That makes the in-process form's blindness DEPENDENT ON TEST ORDERING, which is
worse than a consistent failure: it would pass in CI and fail in isolation, or vice versa. A guard
that can be masked by an unrelated test's import cannot detect the defect it exists for, which is
worse than no guard because it reads as coverage.

WHAT IS OTHERWISE UNGUARDED. A missed registration import is invisible to every existing layer:
the v3 schema treats `uses` as a free-form string (`workflow/schema.py:331`); `lint_workflow`
treats an unknown step as UNKNOWN and SKIPS it by design (`workflow/lint_refs.py:441-447`); and
`tests/unit/workflow/test_contracts.py:186` looks like a completeness guard but iterates
`STEP_REGISTRY` itself, so an op that vanished from the registry is invisible to it. Only a
runtime `WorkflowError` catches it, and only for ops on an executed path.
`plan_review/workflow_ops.py:45` imports `prerequisite_workflow_ops` purely for its side effect,
so a routine "remove unused import" cleanup would silently unregister an op that the shipped
`gates/plan-review.yaml` references at `:136`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAN_REVIEW_YAML = REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates" / "plan-review.yaml"

# This file asserts NO module-size bound. The single authoritative ceiling lives in
# `.github/module-size-limit.txt`, enforced by the CI module-size gate and mirrored in-process by
# test_module_size_contract.py, which reads that same file.

# Eight ops registered by `workflow_ops` itself, plus the ninth registered through its side-effect
# import of `prerequisite_workflow_ops` — the one a stray import cleanup would silently drop.
_EXPECTED_OPS = {
    "plan_review_assemble_criteria",
    "plan_review_coach",
    "plan_review_coach_inputs",
    "plan_review_decide",
    "plan_review_grounding",
    "plan_review_passthrough",
    "plan_review_precheck",
    "plan_review_prerequisite_verify_inputs",
    "plan_review_verify_inputs",
}


def _registered_ops_from_a_clean_interpreter() -> set[str]:
    """The plan-review step names a FIRST import registers, observed in a fresh interpreter.

    Shared by both registry guards (the ticket asks for one helper rather than two). Mirrors the
    subprocess pattern at `tests/unit/test_structured_run_seam.py:115`.
    """
    code = (
        "import json;"
        "from rebar.llm.workflow import steps;"
        "from rebar.llm.workflow.executor import STEP_REGISTRY;"
        "print(json.dumps(sorted(k for k in STEP_REGISTRY if k.startswith('plan_review'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.strip()
    return set(json.loads(out))


def _yaml_uses(path: Path) -> set[str]:
    """Every `uses:` value in a gate definition, walked through `branch`/`else` arms — the coach and
    decide steps live inside branches, so a top-level scan would miss them."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            uses = node.get("uses")
            if isinstance(uses, str):
                found.add(uses)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def test_every_plan_review_op_is_registered_on_a_first_import() -> None:
    """The guard that did not exist anywhere: assert the FULL expected membership, so a dropped
    registration import fails loudly instead of surfacing as a runtime `WorkflowError` on whichever
    path happens to execute first."""
    registered = _registered_ops_from_a_clean_interpreter()
    missing = sorted(_EXPECTED_OPS - registered)
    assert missing == [], (
        f"not registered on a first import: {missing}. A side-effect import was probably removed — "
        "see plan_review/workflow_ops.py:45."
    )


def test_every_uses_in_the_gate_yaml_resolves_to_a_registered_op() -> None:
    """The static check that exists at no layer today: the shipped gate references ops by name, and
    nothing verifies those names resolve. Location-independent by construction, so it survives any
    future move — `plan_review_prerequisite_verify_inputs` already lives in a different module while
    being referenced at `gates/plan-review.yaml:136`, which is the working proof."""
    referenced = {u for u in _yaml_uses(_PLAN_REVIEW_YAML) if u.startswith("plan_review")}
    assert referenced, "no plan_review `uses:` found — the YAML walk itself has broken"
    unresolvable = sorted(referenced - _registered_ops_from_a_clean_interpreter())
    assert unresolvable == [], (
        f"gates/plan-review.yaml references unregistered ops: {unresolvable}. The engine resolves "
        "`uses` against STEP_REGISTRY at runtime (executor.py:539-542), so these would raise "
        "WorkflowError mid-review."
    )
