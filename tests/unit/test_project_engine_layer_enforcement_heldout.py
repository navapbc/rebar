"""HELD-OUT edge suite for `project.engine-layer-enforcement` (task a584, epic 2f4c).

Covers the contract surface the happy-path suite does not: trigger fail-open on
malformed shapes, the scope-TEXT arm (a plan that names the interface path without
declaring file_impact), orchestrator routing integration + gate_log, posture
resolution, prompt/move/eval asset contracts, and the no-leak invariants (packaged
defaults and shipped source unchanged).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import coach_moves, orchestrator, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.review_kernel.coach import applicable_moves

REPO_ROOT = Path(__file__).resolve().parents[2]
CRITERION_ID = "project.engine-layer-enforcement"
MOVE_ID = "project-engine-layer-enforcement"
_ROUTING = REPO_ROOT / ".rebar" / "criteria_routing.json"
_PROMPT = REPO_ROOT / ".rebar" / "prompts" / f"plan-review-{MOVE_ID}.md"
_EVAL = REPO_ROOT / ".rebar" / "evals" / f"plan-review-{MOVE_ID}.eval.yaml"

_FIRE_PLAN = (
    "Guard alias writes against duplicates.\n\n"
    "## Approach\nAdd the duplicate-alias validation so a colliding alias is rejected "
    "at write time in src/rebar/_commands/composer.py.\n\n"
    "## Acceptance Criteria\n- [ ] colliding alias is rejected\n"
)
_NEUTRAL_PLAN = (
    "Improve the wording of two user-facing error strings.\n\n"
    "## Acceptance Criteria\n- [ ] both strings read clearly\n"
)


def _trigger() -> list:
    entry = json.loads(_ROUTING.read_text(encoding="utf-8"))["plan_review"][CRITERION_ID]
    return entry["trigger"]


def _ctx(description: str, *, state: dict | None = None) -> PlanContext:
    return PlanContext(
        ticket_id="abcd-0000-0000-0002",
        ticket_type="task",
        title="T",
        description=description,
        state=state or {},
        repo_root=str(REPO_ROOT),
    )


# ── trigger seam: fail-open + arm coverage ──────────────────────────────────────────
@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        "not-a-list",
        [{"no_arms_here": 1}],
        [{"text_all": []}],
        [42],
    ],
)
def test_trigger_fails_open_on_absent_or_malformed_shapes(malformed):
    """A missing or malformed trigger must return None (route to the LLM as before),
    never raise and never hard-skip the criterion."""
    assert registry.project_trigger_fires(malformed, _FIRE_PLAN, []) is None


def test_scope_text_arm_fires_without_declared_file_impact():
    """A plan that NAMES the interface path in its text (Scope prose) fires the surface
    arm even when no file_impact entries are declared."""
    assert registry.project_trigger_fires(_trigger(), _FIRE_PLAN, []) is True


@pytest.mark.parametrize(
    "path",
    ["src/rebar/_cli/parser.py", "src/rebar/mcp_server.py"],
)
def test_other_interface_surfaces_fire_via_the_glob_arm(path: str):
    """The surface arm is not _commands-only: the CLI package and the MCP server are
    interface surfaces too."""
    plan = (
        "Validate the output format flag.\n\n## Approach\nReject unknown formats.\n\n"
        "## Acceptance Criteria\n- [ ] unknown format rejected\n"
    )
    impact = [{"path": path, "reason": "wire the check"}]
    assert registry.project_trigger_fires(_trigger(), plan, impact) is True


# ── orchestrator integration ────────────────────────────────────────────────────────
def test_fired_plan_routes_the_criterion_into_the_single_tier():
    gate_log: dict[str, str] = {}
    single, agent = orchestrator.route_criteria(_ctx(_FIRE_PLAN), gate_log=gate_log)
    ids = {c["id"] for c in single + agent}
    assert CRITERION_ID in ids
    assert CRITERION_ID not in gate_log


def test_unfired_plan_skips_with_a_gate_log_record():
    gate_log: dict[str, str] = {}
    single, agent = orchestrator.route_criteria(_ctx(_NEUTRAL_PLAN), gate_log=gate_log)
    ids = {c["id"] for c in single + agent}
    assert CRITERION_ID not in ids, (
        "a plan with neither validation vocabulary nor an interface surface must be "
        "deterministically gated out"
    )
    assert CRITERION_ID in gate_log, "the skip must be observable in gate_log"


# ── posture ────────────────────────────────────────────────────────────────────────
def test_criterion_resolves_advisory_never_blocking():
    _threshold, blocking = threshold_for(
        [CRITERION_ID], registry.effective_routing(str(REPO_ROOT)), gate="plan_review"
    )
    assert blocking is False


# ── prompt contract ────────────────────────────────────────────────────────────────
def test_prompt_front_matter_and_finding_contract():
    assert _PROMPT.is_file(), f"criterion prompt missing at {_PROMPT}"
    body = _PROMPT.read_text(encoding="utf-8")
    assert "category: plan-review-criterion" in body
    assert CRITERION_ID in body, "the rubric must name the criterion id for `criteria`"
    assert "engine" in body.lower()


def test_prompt_carries_an_interface_local_non_finding():
    """Advisory means teaching, not nagging: the rubric must carve out legitimately
    interface-local placement (output formatting, arg parsing) as a NON-finding."""
    body = _PROMPT.read_text(encoding="utf-8").lower()
    assert "interface-local" in body or "interface local" in body


# ── move contract ──────────────────────────────────────────────────────────────────
def test_move_is_offered_for_the_criterion_and_not_for_noise():
    moves = coach_moves.load_move_registry(repo_root=str(REPO_ROOT))
    assert MOVE_ID in moves, f"move {MOVE_ID!r} missing from plan_review_moves.json"
    assert "{subject}" in moves[MOVE_ID]["template"]
    assert MOVE_ID in applicable_moves(moves, {CRITERION_ID})
    assert MOVE_ID not in applicable_moves(moves, set())
    assert MOVE_ID not in applicable_moves(moves, {"project.portability"})


# ── eval corpus ────────────────────────────────────────────────────────────────────
def test_eval_corpus_exists_with_fire_and_no_fire_cases():
    assert _EVAL.is_file(), f"eval corpus missing at {_EVAL}"
    doc = yaml.safe_load(_EVAL.read_text(encoding="utf-8"))
    assert doc.get("prompt") == f"plan-review-{MOVE_ID}"
    expects = [row.get("expect") for row in doc.get("dataset", [])]
    assert "finding" in expects, "the corpus needs at least one must-fire case"
    assert "pass" in expects, "the corpus needs at least one must-not-fire case"


# ── no-leak invariants ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        ".rebar/criteria_routing.json",
        ".rebar/plan_review_moves.json",
        f".rebar/prompts/plan-review-{MOVE_ID}.md",
        f".rebar/evals/plan-review-{MOVE_ID}.eval.yaml",
    ],
)
@pytest.mark.allow_unharnessed_subprocess(
    "asks git whether THIS checkout tracks the overlay asset; that is the assertion"
)
def test_every_overlay_asset_is_tracked_by_git(path: str):
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{path} is not tracked by git ({proc.stderr.strip()})"


@pytest.mark.allow_unharnessed_subprocess(
    "greps the real committed src/rebar to prove the overlay id never shipped"
)
def test_criterion_id_is_absent_from_shipped_source():
    """The trigger machinery in src/rebar must stay GENERIC — the project id rides the
    overlay only."""
    proc = subprocess.run(
        ["git", "grep", "-l", CRITERION_ID, "--", "src/rebar"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.strip() == ""


def test_packaged_defaults_and_tracked_guide_are_unchanged():
    assert CRITERION_ID not in registry.effective_criteria(repo_root=None)
    guide = (REPO_ROOT / "docs" / "plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {CRITERION_ID}" not in guide, (
        "the tracked guide is packaged-criteria-only by design; project criteria are "
        "served via `rebar explain`"
    )
