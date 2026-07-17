"""Integration + rubric-contract tests for the `project.portability` plan-review
criterion (epic jira-reb-1003, task immune-floury-toad).

These exercise the REAL committed deliverable at the repo root:
- `.rebar/criteria_routing.json` (activates + routes `project.portability`), and
- `.rebar/prompts/plan-review-project-portability.md` (the Pass-1 rubric).

They point the overlay/prompt/routing machinery at the actual repository root (not a
tmp fixture), so a green run proves the *shipped* config and rubric are wired correctly
— activation, routing scope, production fan-in, and the rubric's mechanical contract.
The "existing behaviour is unchanged when the overlay is absent" acceptance criterion is
covered by the pre-existing `tests/unit/test_criteria_overlay.py` +
`tests/unit/workflow/test_production_batch_runner.py` (both tmp-repo based).
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
CRITERION = "project.portability"
PID = criterion_prompt_id(CRITERION)  # "plan-review-project-portability"
_PROMPT_FILE = Path(REPO_ROOT) / ".rebar" / "prompts" / f"{PID}.md"


@pytest.fixture(autouse=True)
def _clear_caches():
    # The overlay/prompt views are content-signature memoized; clear around each test so
    # the real repo-root overlay is (re)read and never served stale.
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


def _rubric_body() -> str:
    """The rubric body as resolved by the production prompt loader (front-matter stripped)."""
    return prompts.get_prompt(PID, repo_root=REPO_ROOT).text


# ── activation + routing metadata ────────────────────────────────────────────────
def test_criterion_activation():
    """The committed overlay activates `project.portability` with the exact routing the
    plan specifies (both `criterion_activation` acceptance criteria)."""
    assert CRITERION in registry.effective_criteria(REPO_ROOT)
    routing = registry.effective_routing(REPO_ROOT)[CRITERION]
    assert routing["exec"] == "1-TURN"
    assert routing["facet"] == "project-invariants"
    assert routing["applies_at"]["scope"] == ["container", "leaf"]
    assert routing["default_posture"] == "blocking"
    assert routing["block_threshold"] == 0.9


# ── prompt front-matter + resolution ─────────────────────────────────────────────
def test_prompt_frontmatter():
    """The rubric declares the exact six front-matter values fixed in the plan."""
    meta, _ = parse_front_matter(_PROMPT_FILE.read_text(encoding="utf-8"))
    assert meta["schema_version"] == 1
    assert meta["title"] == "Rebar portability"
    assert (
        meta["description"]
        == "Find concrete rebar portability failures across supported client shapes."
    )
    assert meta["execution_mode"] == "single_turn"
    assert meta["category"] == "plan-review-criterion"
    assert meta["dimension"] == "project-invariants"


def test_prompt_resolution():
    """The rubric resolves through the production prompt loader for `project.portability`
    (the `.rebar` override wins), yielding the authored identity."""
    p = prompts.get_prompt(PID, repo_root=REPO_ROOT)
    assert p.title == "Rebar portability"
    assert p.execution_mode == "single_turn"
    assert p.category == "plan-review-criterion"
    assert p.dimension == "project-invariants"


# ── proportionate-scrutiny routing (leaf + container) ────────────────────────────
def test_leaf_routing():
    """A leaf (childless) plan fans `project.portability` into the Pass-1 single-turn set."""
    ctx = PlanContext(
        ticket_id="abcd-0000-0000-0001",
        ticket_type="task",
        title="A leaf task",
        description="## Acceptance Criteria\n- [ ] do the thing\n" + "x" * 200,
        repo_root=REPO_ROOT,
    )
    assert ctx.has_children is False
    single, _agent = route_criteria(ctx)
    assert CRITERION in {c["id"] for c in single}


def test_container_routing():
    """A container (has-children) plan also routes `project.portability` (scope covers both)."""
    ctx = PlanContext(
        ticket_id="abcd-0000-0000-0002",
        ticket_type="epic",
        title="A container epic",
        description="## Success Criteria\n- [ ] shipped\n\n## Acceptance Criteria\n"
        "- [ ] all child stories closed\n" + "x" * 200,
        children=[{"ticket_id": "c000-0000-0000-0001", "ticket_type": "task", "status": "open"}],
        repo_root=REPO_ROOT,
    )
    assert ctx.has_children is True
    single, _agent = route_criteria(ctx)
    assert CRITERION in {c["id"] for c in single}


# ── production fan-in surfaces a finding (offline, FakeRunner) ────────────────────
def test_production_fan_in(tmp_path, monkeypatch):
    """The real ProductionBatchRunner fans the activated project criterion into the finder
    set and surfaces its finding — with NO built-ins passed and a FakeRunner (no billable
    call). This is the end-to-end activate→route→run→surface proof.

    The SHIPPED overlay + rubric are copied byte-for-byte into a sandboxed repo so the
    runner's cache writes stay under tmp_path — the conftest hygiene guard forbids leaking
    new entries into REPO_ROOT. The copy still validates the committed content exactly.
    """
    import shutil

    from rebar import config as _config
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
    from rebar.llm.runner import FakeRunner
    from rebar.llm.workflow.runners import BatchRunRequest

    src = Path(REPO_ROOT)
    (tmp_path / ".rebar" / "prompts").mkdir(parents=True)
    shutil.copy(
        src / ".rebar" / "criteria_routing.json",
        tmp_path / ".rebar" / "criteria_routing.json",
    )
    shutil.copy(
        src / ".rebar" / "prompts" / f"{PID}.md",
        tmp_path / ".rebar" / "prompts" / f"{PID}.md",
    )

    # Overlay discovery keys off config.repo_root() inside the runner's context builder.
    monkeypatch.setattr(_config, "repo_root", lambda *a, **k: tmp_path)
    prompt_library._invalidate_caches()

    state = {
        "ticket_id": "abcd-0000-0000-0001",
        "ticket_type": "story",
        "title": "Land the thing via a GitHub PR",
        "description": "## Why\nx\n\n## What\nbuild X\n\n## Acceptance Criteria\n- [ ] x is true\n",
        "deps": [],
    }
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, *, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent=None, repo_root=None: [])

    fake = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {
                    "finding": "plan assumes a GitHub-PR landing path",
                    "criteria": [CRITERION],
                }
            ],
        }
    )
    req = BatchRunRequest(
        finder="plan-review-finder",
        criteria=(),  # NO built-ins — the project criterion must be fanned in by the runner
        usd_budget=None,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket="abcd-0000-0000-0001",
        repo_root=str(tmp_path),
        run_id="run-1",
        step_id="finders",
    )
    result = ProductionBatchRunner(runner=fake).run(req, None)

    assert result.outputs["batch_plan"]["batch_resolution"]["project"] == [CRITERION]
    surfaced = [f for f in result.outputs["findings"] if CRITERION in (f.get("criteria") or [])]
    assert surfaced, (
        f"expected the project criterion to surface a finding; got {result.outputs['findings']}"
    )


# ── rubric mechanical contract (headings + literal rules) ─────────────────────────
def test_finding_threshold():
    """`## Finding threshold` requires all four counterexample elements before a finding."""
    body = _rubric_body()
    assert "## Finding threshold" in body
    for element in (
        "cited plan mechanism",
        "materially different supported client shape",
        "causal failure mechanism",
        "observable breakage scenario",
    ):
        assert element in body, f"missing Finding-threshold element: {element!r}"


def test_finding_schema_markers():
    """`## Required finding fields` names the five typed field markers."""
    body = _rubric_body()
    assert "## Required finding fields" in body
    for marker in (
        "location: str",
        "finding: str",
        "scenarios: list[str]",
        "evidence: list[str]",
        "criteria: list[str]",
    ):
        assert marker in body, f"missing Required-finding-fields marker: {marker!r}"


def test_client_shape_matrix():
    """`## Supported client-shape matrix` contains every exact label + value pair."""
    body = _rubric_body()
    assert "## Supported client-shape matrix" in body
    matrix = {
        "Harness": "Python library, CLI, remote MCP; no Claude Code or Codex dependency.",
        "Target project": "Ruby, Python, Java, Next.js, .NET, Terraform subprojects in a monorepo.",
        "Platform and venue": "macOS, Windows, Linux, BSD, CI, servers, developer workstations.",
        "Project location and access": (
            "in-checkout current working directory, explicitly located workspace, "
            "server outside the checkout, no unrestricted-local-filesystem assumption."
        ),
    }
    for label, value in matrix.items():
        assert label in body, f"missing client-shape label: {label!r}"
        assert value in body, f"missing client-shape value for {label!r}: {value!r}"


def test_omission_guard():
    """The rubric carries the exact omission non-finding rule."""
    body = _rubric_body()
    assert "## Non-findings" in body
    assert "Silence about portability is not a finding" in body


def test_project_configuration_guard():
    """The rubric carries the exact project-configuration non-finding rule."""
    body = _rubric_body()
    assert (
        "Project-specific behavior behind project configuration or an explicit "
        "extension boundary is allowed"
    ) in body
