"""Guard shipped plan-review workflow op registration in a fresh subprocess. Registering modules
populate a process-global registry on import, so another test can mask a missing production
side-effect import. Schema lint permits unknown ops and registry-based completeness checks
cannot see vanished entries. The subprocess catches the missing prerequisite op."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.allow_unharnessed_subprocess(
    "the shared helper imports the shipped package in a clean interpreter rooted at\n"
    "the checkout to read the REAL step registry"
)

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
