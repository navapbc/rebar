"""Offline contract tests for the project-owned `project.engine-layer-enforcement`
plan-review criterion (task a584, epic 2f4c): ADVISORY posture, routed by a
deterministic conjunction trigger (interface-surface paths AND validation/guard
vocabulary) carried in the `.rebar/criteria_routing.json` overlay entry.

Exercises the REAL committed `.rebar/` overlay at the repo root — no model call, no
network. The live fire/no-fire semantic demonstration is the eval corpus
(`.rebar/evals/plan-review-project-engine-layer-enforcement.eval.yaml`).
"""

from __future__ import annotations

import json
from pathlib import Path

from rebar.llm.plan_review import registry

REPO_ROOT = Path(__file__).resolve().parents[2]
CRITERION_ID = "project.engine-layer-enforcement"
_ROUTING = REPO_ROOT / ".rebar" / "criteria_routing.json"

_VALIDATION_PLAN = (
    "Add duplicate-alias validation when a ticket alias is set.\n\n"
    "## Approach\nReject a colliding alias with a clear error at write time.\n\n"
    "## Acceptance Criteria\n- [ ] colliding alias is rejected\n"
)
_NEUTRAL_PATHS_PLAN = (
    "Rewrite two long help strings for the create command.\n\n"
    "## Scope\nsrc/rebar/_commands/composer.py wording only.\n\n"
    "## Acceptance Criteria\n- [ ] help text reads clearly\n"
)
_COMMANDS_IMPACT = [{"path": "src/rebar/_commands/composer.py", "reason": "add the validation"}]


def _entry() -> dict:
    return json.loads(_ROUTING.read_text(encoding="utf-8"))["plan_review"][CRITERION_ID]


def test_criterion_is_active_advisory_and_plan_review_only():
    """The overlay registers the criterion: active for plan review, ADVISORY posture,
    project-invariants facet, activated for the plan_review gate alone."""
    assert CRITERION_ID in registry.effective_criteria(str(REPO_ROOT))
    entry = _entry()
    assert entry.get("default_posture") == "advisory"
    assert entry.get("facet") == "project-invariants"
    routing = json.loads(_ROUTING.read_text(encoding="utf-8"))
    assert routing["activate"][CRITERION_ID] == ["plan_review"]


def test_committed_trigger_is_a_conjunction_over_work_and_surface():
    """The routing entry's deterministic trigger fires only when BOTH hold: the plan
    names validation/guard/check work AND an interface surface is in play (declared
    file_impact under src/rebar/_commands/ here). Either half alone must not fire."""
    trigger = _entry().get("trigger")
    assert trigger, "routing entry must carry the deterministic trigger"
    fires = registry.project_trigger_fires
    assert fires(trigger, _VALIDATION_PLAN, _COMMANDS_IMPACT) is True
    assert fires(trigger, _VALIDATION_PLAN, []) is False, (
        "validation vocabulary with no interface surface must not fire"
    )
    assert fires(trigger, _NEUTRAL_PATHS_PLAN, _COMMANDS_IMPACT) is False, (
        "an interface path with no validation/guard vocabulary must not fire"
    )


def test_explain_serves_the_detection_detail():
    """`rebar explain project.engine-layer-enforcement` renders the criterion section
    (detection detail) from the overlay prompt — the project analog of the generated
    guide sections."""
    section = registry.explain_criterion(CRITERION_ID, repo_root_path=str(REPO_ROOT))
    text = section if isinstance(section, str) else str(section)
    assert CRITERION_ID in text
    assert "engine" in text.lower()
