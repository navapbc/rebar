"""Held-out behavioral contracts for the no-file-impact criterion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar._cli import main
from rebar.llm.config import LLMConfig
from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals import eval as eval_runtime
from rebar.llm.plan_review import orchestrator, pass1, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.prompting import prompts

pytestmark = pytest.mark.unit

_CID = "no-file-impact"
_REASON_HEADING = "## Declared no-file-impact context"


def _ctx(
    scope: str | None,
    *,
    reason: str | None = "operator action outside the repository",
    children: list[dict] | None = None,
    repo_root: str | None = None,
) -> PlanContext:
    state: dict[str, object] = {"file_impact_scope": scope}
    if reason is not None:
        state["no_file_impact_reason"] = reason
    return PlanContext(
        ticket_id="scope-0000-0000-0002",
        ticket_type="story",
        title="External operator action",
        description=(
            "## Plan\n"
            "Ask the release operator to rotate an external credential.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] The external credential is rotated.\n"
        ),
        state=state,
        children=children or [],
        repo_root=repo_root,
    )


def _routed_ids(scope: str | None) -> set[str]:
    single, agent = orchestrator.route_criteria(_ctx(scope))
    return {criterion["id"] for criterion in [*single, *agent]}


def test_declared_none_routes_loadable_advisory_criterion() -> None:
    single, agent = orchestrator.route_criteria(_ctx("none"))
    routed = {criterion["id"]: criterion for criterion in [*single, *agent]}

    criterion = routed[_CID]
    assert criterion["default_posture"] == "advisory"
    assert criterion["block_threshold"] == 0.95
    assert criterion in single

    prompt = prompts.get_prompt(criterion_prompt_id(_CID))
    assert prompt.category == "plan-review-criterion"
    assert prompt.dimension == "scope-intent"


def test_only_authenticated_none_scope_routes_criterion() -> None:
    assert _CID in _routed_ids("none")
    for scope in ("paths", "undeclared", "future-scope", None):
        assert _CID not in _routed_ids(scope)


@pytest.mark.parametrize(
    "bad_value",
    [
        [],
        [""],
        ["none", ""],
        [1],
        "none",
    ],
)
def test_public_overlay_loader_rejects_malformed_scope_filters(
    tmp_path: Path,
    bad_value: object,
) -> None:
    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir()
    (rebar_dir / "criteria_routing.json").write_text(
        json.dumps(
            {
                "plan_review": {
                    _CID: {
                        "applies_at": {
                            "require_file_impact_scope": bad_value,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(registry.RegistryError, match="require_file_impact_scope"):
        registry.effective_routing(str(tmp_path))


class _CaptureRunner:
    name = "capture"

    def __init__(self) -> None:
        self.requests: list[object] = []

    def preflight(self) -> None:
        return None

    def run(self, request):
        self.requests.append(request)
        return {"findings": []}


def _criterion_request(runner: _CaptureRunner):
    return next(request for request in runner.requests if _CID in request.instructions)


def _declared_reason_from(request) -> str:
    lines = request.instructions.splitlines()
    heading_index = lines.index(_REASON_HEADING)
    return json.loads(lines[heading_index + 1])["declared_reason"]


def test_pass1_preserves_reason_and_composes_same_facet_context(
    tmp_path: Path,
) -> None:
    reason = 'Operator says "rotate now".\nTarget: café ☕'
    children = [
        {
            "ticket_id": "child-1",
            "alias": "external-step",
            "title": "Perform external rotation",
            "status": "open",
        }
    ]
    runner = _CaptureRunner()
    criteria = registry.by_id()

    pass1.run_pass1(
        _ctx(
            "none",
            reason=reason,
            children=children,
            repo_root=str(tmp_path),
        ),
        LLMConfig(runner="fake"),
        runner,
        [criteria["G5"], criteria[_CID]],
        [],
        {},
    )

    request = _criterion_request(runner)
    assert _declared_reason_from(request) == reason
    assert request.instructions.index("DECOMPOSITION STATE (from store)") < (
        request.instructions.index(_REASON_HEADING)
    )


def test_pass1_serializes_missing_reason_as_empty_string(tmp_path: Path) -> None:
    runner = _CaptureRunner()
    criterion = registry.by_id()[_CID]

    pass1.run_pass1(
        _ctx("none", reason=None, repo_root=str(tmp_path)),
        LLMConfig(runner="fake"),
        runner,
        [criterion],
        [],
        {},
    )

    assert _declared_reason_from(_criterion_request(runner)) == ""


def test_public_explain_surface_resolves_generated_criterion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["explain", _CID]) == 0
    output = capsys.readouterr().out
    assert output.startswith(f"## {_CID}")
    assert registry.explain_criterion(_CID) == output.rstrip()


def test_criteria_guide_regeneration_is_canonical_and_idempotent(
    tmp_path: Path,
) -> None:
    docs_dir = tmp_path / "docs"
    prompts_dir = tmp_path / ".rebar" / "prompts"
    docs_dir.mkdir()
    prompts_dir.mkdir(parents=True)
    (tmp_path / ".rebar" / "criteria_routing.json").write_text(
        json.dumps(
            {
                "plan_review": {
                    "project.example": {
                        "exec": "1-TURN",
                        "facet": "project-invariants",
                        "default_posture": "advisory",
                        "checklist": [{"key": "example", "check": "Evaluate the example."}],
                    }
                },
                "activate": {"project.example": ["plan_review"]},
            }
        ),
        encoding="utf-8",
    )
    (prompts_dir / "plan-review-project-example.md").write_text(
        (
            "---\n"
            "schema_version: 1\n"
            "title: Project example\n"
            "description: Project-local criterion.\n"
            "execution_mode: single_turn\n"
            "category: plan-review-criterion\n"
            "dimension: project-invariants\n"
            "---\n"
            "Evaluate the project-specific invariant.\n"
        ),
        encoding="utf-8",
    )

    guide_path = Path(registry.regenerate_criteria_guide(str(tmp_path)))
    first = guide_path.read_bytes()
    registry.regenerate_criteria_guide(str(tmp_path))

    assert guide_path.read_bytes() == first
    assert registry.validate_criteria_guide(str(tmp_path)) == []
    assert registry.explain_criterion("project.example", repo_root_path=str(tmp_path)).startswith(
        "## project.example"
    )


def test_bounded_eval_contract_covers_pass_contradictions_and_insufficient() -> None:
    spec = eval_runtime.load_eval_spec(f"plan-review-{_CID}")

    assert eval_runtime.validate_eval_spec(spec, strict=True) == []
    cases = {case["id"]: case["expect"] for case in spec["dataset"]}
    assert cases == {
        "NFI-P1-external-operator-action": "pass",
        "NFI-F1-source-change": "finding",
        "NFI-F2-docs-change": "finding",
        "NFI-F3-insufficient-reason": "finding",
    }
