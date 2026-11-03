"""Structural tests for the `project.net-mechanism` plan-review criterion (ticket
fe94-2e22-ea9e-46f7, epic 77e3-28e2-31ec-47ec).

These exercise the REAL committed deliverable at the repo root:
- `.rebar/criteria_routing.json` (activates + routes `project.net-mechanism`), and
- `.rebar/prompts/plan-review-project-net-mechanism.md` (the Pass-1 rubric).

They are STRUCTURAL ONLY by design. Nothing here executes model judgment, so no test
asserts that a given plan is or is not scored as a finding: such a test could only assert
a canned score, which proves the fixture rather than the criterion. Firing behaviour is
proven by the calibration corpus in child ticket 39bc, against real model runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.plan_review import registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.orchestrator import route_criteria
from rebar.llm.prompting import prompt_library, prompts
from rebar.llm.prompting.prompts_frontmatter import parse_front_matter

REPO_ROOT = str(Path(__file__).resolve().parents[2])
CRITERION = "project.net-mechanism"
PID = criterion_prompt_id(CRITERION)  # "plan-review-project-net-mechanism"
_PROMPT_FILE = Path(REPO_ROOT) / ".rebar" / "prompts" / f"{PID}.md"

# The closed seven-kind census the rubric must enumerate inline. The criterion runs at
# claim time with no tree access, so it has no runtime source for this taxonomy and must
# carry it in the body.
COUNTED_KINDS = (
    "lock",
    "env_var",
    "config_key",
    "feature_flag",
    "ci_gate",
    "autouse_fixture",
    "test_helper",
)

# Naming a CI vendor would make the rubric non-portable: `project.portability` requires a
# capability to work with any CI provider or none, and the `ci_gate` kind is recognised
# from plan prose, which needs no CI present.
CI_VENDORS = ("GitHub Actions", "GitLab", "Jenkins", "CircleCI", "Travis")


# The overlay/prompt views are content-signature memoized. Every read below drops the
# memo first so the real repo-root overlay is re-read and never served stale.
#
# This is deliberately a plain function rather than an autouse fixture. An autouse fixture
# is one of the seven mechanism kinds this very criterion counts, and the mechanism-delta
# ratchet flagged it as a new, unjustified mechanism when this module first carried one.
# Marking it admitted would have added a mechanism to the change whose entire purpose is
# to make additions justify themselves. Calling the invalidation directly costs nothing
# and adds no counted mechanism.
def _invalidate() -> None:
    prompt_library._invalidate_caches()


def _rubric_body() -> str:
    """The rubric body as resolved by the production prompt loader (front-matter stripped)."""
    _invalidate()
    return prompts.get_prompt(PID, repo_root=REPO_ROOT).text


def _routing() -> dict:
    """This criterion's committed routing entry, read fresh."""
    _invalidate()
    return registry.effective_routing(REPO_ROOT)[CRITERION]


# ── activation + routing metadata ────────────────────────────────────────────────
def test_criterion_is_activated():
    _invalidate()
    assert CRITERION in registry.effective_criteria(REPO_ROOT)


def test_criterion_posture_is_blocking():
    assert _routing()["default_posture"] == "blocking"


def test_criterion_routing_metadata():
    """The declared scope the `route_criteria` tests below target."""
    routing = _routing()
    assert routing["exec"] == "1-TURN"
    assert routing["facet"] == "project-invariants"
    assert routing["applies_at"]["scope"] == ["container", "leaf"]
    assert routing["block_threshold"] == 0.9


# ── prompt resolution + front matter ─────────────────────────────────────────────
def test_prompt_resolves_to_a_non_empty_body():
    assert _rubric_body().strip()


def test_prompt_frontmatter_yields_all_six_keys():
    meta, _ = parse_front_matter(_PROMPT_FILE.read_text(encoding="utf-8"))
    assert meta["schema_version"] == 1
    assert meta["title"] == "Rebar net mechanism"
    assert meta["description"] == (
        "Find plans that add a counted mechanism without naming a retirement "
        "or why none is possible."
    )
    assert meta["execution_mode"] == "single_turn"
    assert meta["category"] == "plan-review-criterion"
    assert meta["dimension"] == "project-invariants"


# ── proportionate-scrutiny routing (both declared scopes) ────────────────────────
def test_leaf_routing():
    """A leaf (childless) plan fans the criterion into the Pass-1 single-turn set."""
    _invalidate()
    ctx = PlanContext(
        ticket_id="abcd-0000-0000-0011",
        ticket_type="task",
        title="A leaf task",
        description="## Acceptance Criteria\n- [ ] do the thing\n" + "x" * 200,
        repo_root=REPO_ROOT,
    )
    assert ctx.has_children is False
    single, _agent = route_criteria(ctx)
    assert CRITERION in {c["id"] for c in single}


def test_container_routing():
    """A container (has-children) plan routes it too; the declared scope covers both."""
    _invalidate()
    ctx = PlanContext(
        ticket_id="abcd-0000-0000-0012",
        ticket_type="epic",
        title="A container epic",
        description="## Acceptance Criteria\n- [ ] all child stories closed\n" + "x" * 200,
        children=[{"ticket_id": "c000-0000-0000-0011", "ticket_type": "task", "status": "open"}],
        repo_root=REPO_ROOT,
    )
    assert ctx.has_children is True
    single, _agent = route_criteria(ctx)
    assert CRITERION in {c["id"] for c in single}


# ── rubric mechanical contract (headings + literal rules) ────────────────────────
def test_rubric_has_finding_threshold_heading():
    assert "## Finding threshold" in _rubric_body()


def test_rubric_has_required_finding_fields_heading():
    assert "## Required finding fields" in _rubric_body()


def test_rubric_has_non_findings_heading():
    assert "## Non-findings" in _rubric_body()


@pytest.mark.parametrize("kind", COUNTED_KINDS)
def test_rubric_names_each_counted_kind(kind):
    assert f"`{kind}`" in _rubric_body()


@pytest.mark.parametrize("vendor", CI_VENDORS)
def test_rubric_names_no_ci_vendor(vendor):
    assert vendor not in _rubric_body()


def test_rubric_requires_only_schema_backed_finding_fields():
    """Every field the rubric demands must exist on the Pass-1 finding model, or the
    harvester drops it silently. This is the exact defect the plan review caught in this
    plan's own first draft, which named a `mechanisms` field the schema does not carry.

    The finding model is defined inside the `_pass1_model()` factory rather than at module
    level, so it is reached through the output model's `findings` annotation.
    """
    from rebar.llm.plan_review.passes import _pass1_model

    finding_model = _pass1_model().model_fields["findings"].annotation.__args__[0]

    body = _rubric_body()
    schema_fields = set(finding_model.model_fields)
    demanded = {"location", "finding", "scenarios", "evidence", "criteria"}
    assert demanded <= schema_fields
    assert "`mechanisms`" not in body
