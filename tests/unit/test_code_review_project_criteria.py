"""Behavioral coverage for project-owned code-review criteria."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar.llm.code_review import registry as code_review_registry
from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.config import LLMConfig
from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.criteria.model import CriteriaError
from rebar.llm.errors import LLMError
from rebar.llm.evals import eval as _eval
from rebar.llm.evals import eval_solver
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import gate_dispatch
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers workflow ops
from rebar.llm.workflow.runners import BatchRunRequest

_PROJECT_ID = "project.foo"
_PROJECT_PROMPT = "code-review-project-foo"
_PLAN_PROJECT_PROMPT = "plan-review-project-foo"
_PROJECT_RUBRIC = """\
---
schema_version: 1
title: Project foo review
description: Project-owned code-review finder used by the runtime seam contract.
outputs: code_review_findings
execution_mode: agentic
category: code-review-pass
dimension: project-foo
---
Find project foo violations in the supplied change.
"""
_PLAN_PROJECT_RUBRIC = """\
---
schema_version: 1
title: Project foo plan review
description: Project-owned plan-review finder used by the ambiguity contract.
outputs: plan_review_findings
execution_mode: single_turn
category: plan-review-criterion
dimension: project-foo
---
Find project foo violations in the supplied plan.
"""
_PROJECT_ROUTING = {
    "exec": "1-TURN",
    "applies_to": [],
    "default_posture": "advisory",
    "block_threshold": 0.8,
    "blocking_enabled": False,
}
_PLAN_PROJECT_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}


def test_resolver_maps_project_criterion_into_code_review_namespace() -> None:
    assert criterion_prompt_id("project.foo", gate_key="code_review") == "code-review-project-foo"


def test_resolver_maps_builtin_criterion_into_code_review_namespace() -> None:
    assert criterion_prompt_id("F1", gate_key="code_review") == "code-review-F1"


def test_resolver_preserves_default_and_rejects_unknown_gate() -> None:
    assert criterion_prompt_id("F1") == "plan-review-F1"
    assert criterion_prompt_id("project.foo") == "plan-review-project-foo"

    with pytest.raises(CriteriaError, match="unknown gate"):
        criterion_prompt_id("project.foo", gate_key="completion")


def test_resolver_keeps_gate_namespaces_disjoint() -> None:
    plan_review_id = criterion_prompt_id("project.foo")
    code_review_id = criterion_prompt_id("project.foo", gate_key="code_review")

    assert plan_review_id == "plan-review-project-foo"
    assert code_review_id == "code-review-project-foo"
    assert plan_review_id != code_review_id


class _RecordingBatchAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, ctx):
        prompt_id = str(ctx.step["prompt"])
        self.calls.append((ctx.step_id, prompt_id))
        return _ex.StepResult(
            outputs={
                "findings": [
                    {
                        "finding": "project finding",
                        "criteria": ["existing-tag"],
                        "evidence": ["x.py:1"],
                        "location": "x.py:1",
                    }
                ]
            }
        )


def _batch_request(*, step_id: str, repo_root: str | None = None) -> BatchRunRequest:
    return BatchRunRequest(
        finder="code-review-base",
        criteria=(),
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=repo_root,
        run_id="project-criteria-test",
        step_id=step_id,
    )


def _project_entries() -> tuple[dict[str, str], ...]:
    return ({"criterion_id": _PROJECT_ID, "prompt": _PROJECT_PROMPT},)


def test_project_criterion_fan_in_runs_in_round_a_with_logical_attribution(tmp_path) -> None:
    prompts = tmp_path / ".rebar" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{_PROJECT_PROMPT}.md").write_text(_PROJECT_RUBRIC, encoding="utf-8")
    agent = _RecordingBatchAgent()
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_project_entries(),
        project_criteria_root=str(tmp_path),
    )

    result = runner.run(_batch_request(step_id="round_a", repo_root=str(tmp_path)), agent)

    assert agent.calls == [("round_a:code-review-project-foo", _PROJECT_PROMPT)]
    assert result.outputs["criteria_count"] == 1
    assert result.outputs["batch_plan"]["ran"] == [_PROJECT_PROMPT]
    assert result.outputs["findings"] == [
        {
            "finding": "project finding",
            "criteria": ["existing-tag", _PROJECT_ID],
            "evidence": ["x.py:1"],
            "location": "x.py:1",
            "reviewer_id": _PROJECT_PROMPT,
        }
    ]


@pytest.mark.parametrize(
    ("emitted_criteria", "expected"),
    [
        (None, [_PROJECT_ID]),
        ([_PROJECT_ID], [_PROJECT_ID]),
    ],
)
def test_project_criterion_logical_attribution_is_present_once(
    tmp_path,
    emitted_criteria,
    expected,
) -> None:
    prompts = tmp_path / ".rebar" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{_PROJECT_PROMPT}.md").write_text(_PROJECT_RUBRIC, encoding="utf-8")

    class _AttributionAgent:
        def run(self, ctx):
            finding = {
                "finding": "project finding",
                "evidence": ["x.py:1"],
                "location": "x.py:1",
            }
            if emitted_criteria is not None:
                finding["criteria"] = list(emitted_criteria)
            return _ex.StepResult(outputs={"findings": [finding]})

    result = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_project_entries(),
        project_criteria_root=str(tmp_path),
    ).run(
        _batch_request(step_id="round_a", repo_root=str(tmp_path)),
        _AttributionAgent(),
    )

    assert result.outputs["findings"][0]["criteria"] == expected


def test_malformed_internal_project_entry_is_skipped() -> None:
    agent = _RecordingBatchAgent()
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=(
            {"criterion_id": _PROJECT_ID},
            {"prompt": _PROJECT_PROMPT},
        ),
    )

    result = runner.run(_batch_request(step_id="round_a"), agent)

    assert agent.calls == []
    assert result.outputs["criteria_count"] == 0
    assert result.outputs["batch_plan"]["ran"] == []


def test_project_criterion_fan_in_is_round_a_only_and_replay_stable(tmp_path) -> None:
    prompts = tmp_path / ".rebar" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{_PROJECT_PROMPT}.md").write_text(_PROJECT_RUBRIC, encoding="utf-8")
    agent = _RecordingBatchAgent()
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_project_entries(),
        project_criteria_root=str(tmp_path),
    )

    round_b = runner.run(_batch_request(step_id="round_b", repo_root=str(tmp_path)), agent)
    first = runner.run(_batch_request(step_id="round_a", repo_root=str(tmp_path)), agent)
    second = runner.run(_batch_request(step_id="round_a", repo_root=str(tmp_path)), agent)

    assert round_b.outputs["criteria_count"] == 0
    assert round_b.outputs["batch_plan"]["ran"] == []
    assert [prompt for _, prompt in agent.calls] == [_PROJECT_PROMPT, _PROJECT_PROMPT]
    assert first.outputs == second.outputs


def test_project_criterion_missing_prompt_raises_located_llm_error(tmp_path) -> None:
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_project_entries(),
        project_criteria_root=str(tmp_path),
    )

    with pytest.raises(
        LLMError,
        match=r"project\.foo.*\.rebar/prompts/code-review-project-foo\.md",
    ):
        runner.run(
            _batch_request(step_id="round_a", repo_root=str(tmp_path)),
            _RecordingBatchAgent(),
        )


def test_project_criterion_wrong_output_contract_raises_located_llm_error(tmp_path) -> None:
    prompts = tmp_path / ".rebar" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / f"{_PROJECT_PROMPT}.md").write_text(
        _PROJECT_RUBRIC.replace(
            "outputs: code_review_findings",
            "outputs: review_result",
        ),
        encoding="utf-8",
    )
    runner = CodeReviewBatchRunner(
        context="DIFF",
        project_criteria=_project_entries(),
        project_criteria_root=str(tmp_path),
    )

    with pytest.raises(
        LLMError,
        match=r"project\.foo.*\.rebar/prompts/code-review-project-foo\.md.*"
        r"expected outputs 'code_review_findings', got 'review_result'",
    ):
        runner.run(
            _batch_request(step_id="round_a", repo_root=str(tmp_path)),
            _RecordingBatchAgent(),
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _project_repo(
    tmp_path: Path,
    *,
    with_prompt: bool = True,
    routing: dict | None = None,
) -> Path:
    repo = tmp_path / "repo"
    prompt_dir = repo / ".rebar" / "prompts"
    prompt_dir.mkdir(parents=True)
    (repo / ".rebar" / "criteria_routing.json").write_text(
        json.dumps(
            {
                "code_review": {_PROJECT_ID: routing or _PROJECT_ROUTING},
                "activate": [_PROJECT_ID],
            }
        ),
        encoding="utf-8",
    )
    if with_prompt:
        (prompt_dir / f"{_PROJECT_PROMPT}.md").write_text(_PROJECT_RUBRIC, encoding="utf-8")
    (repo / "x.py").write_text("print('changed')\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Project Criteria Test")
    _git(repo, "config", "user.email", "project-criteria@example.test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _write_eval_spec(repo: Path, prompt_id: str) -> None:
    evals = repo / ".rebar" / "evals"
    evals.mkdir(parents=True, exist_ok=True)
    (evals / f"{prompt_id}.eval.yaml").write_text(
        json.dumps(
            {
                "prompt": prompt_id,
                "model": "anthropic:claude-sonnet-4-6",
                "epochs": 1,
                "gate": "at_least(1)",
                "coverage_threshold": 1.0,
                "scorers": [
                    {
                        "type": "deterministic",
                        "name": "code_review_emits_valid_findings",
                    }
                ],
                "dataset": [
                    {
                        "id": "project-fire",
                        "expect": "finding",
                        "diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('changed')\n",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _activate_plan_review_project_criterion(repo: Path) -> None:
    routing_path = repo / ".rebar" / "criteria_routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["plan_review"] = {_PROJECT_ID: _PLAN_PROJECT_ROUTING}
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    (repo / ".rebar" / "prompts" / f"{_PLAN_PROJECT_PROMPT}.md").write_text(
        _PLAN_PROJECT_RUBRIC,
        encoding="utf-8",
    )


class _ProductionRecordingRunner:
    name = "project-criteria-test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):
        prompt_id = req.reviewers[0]
        self.calls.append(prompt_id)
        self.requests.append(req)
        if prompt_id == "code-review-base":
            return {
                "findings": [],
                "recommend_overlays": [{"overlay_id": "tests", "reason": "exercise Round B"}],
            }
        if prompt_id == _PROJECT_PROMPT:
            return {
                "findings": [
                    {
                        "finding": "project production finding",
                        "criteria": ["existing-tag"],
                        "evidence": ["x.py:1"],
                        "location": "x.py:1",
                    }
                ]
            }
        if prompt_id.startswith("code-review-") and prompt_id not in {
            "code-review-verify",
            "code-review-coach",
        }:
            return {"findings": []}
        if prompt_id == "code-review-verify":
            return {
                "verifications": [
                    {
                        "index": 0,
                        "binary": {
                            "is_verifiable": "yes",
                            "evidence_entails_finding": "yes",
                            "path_reachable": "yes",
                            "impact_follows_necessarily": "yes",
                            "no_viable_alternative_explanation": "yes",
                            "no_existing_mitigation": "yes",
                            "severity_claim_justified": "yes",
                        },
                        "severity_attributes": {},
                    }
                ]
            }
        if prompt_id == "code-review-coach":
            return {"notes": []}
        return {}


def test_eval_code_review_prompt_id_admits_active_project(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    active = code_review_registry.effective_criteria(str(repo))
    assert _PROJECT_ID in active

    assert (
        eval_solver._code_review_prompt_id(_PROJECT_PROMPT, repo_root=str(repo)) == _PROJECT_PROMPT
    )


def test_eval_calibrate_active_project_criterion_through_code_review_arm(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    _write_eval_spec(repo, _PROJECT_PROMPT)
    runner = _ProductionRecordingRunner()

    result = _eval.calibrate_criterion(
        _PROJECT_ID,
        repo_root=str(repo),
        runner=runner,
    )

    assert result["prompt"] == _PROJECT_PROMPT
    assert (result["n_fire"], result["recall"]) == (1, 1.0)
    assert runner.calls == [_PROJECT_PROMPT]


def test_eval_code_review_prompt_id_rejects_inactive_project(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    inactive_id = "project.inactive"
    assert inactive_id not in code_review_registry.effective_criteria(str(repo))

    assert (
        eval_solver._code_review_prompt_id(
            criterion_prompt_id(inactive_id, gate_key="code_review"),
            repo_root=str(repo),
        )
        is None
    )


def test_eval_code_review_prompt_id_keeps_builtins_with_or_without_repo_root(tmp_path) -> None:
    assert eval_solver._code_review_prompt_id("code-review-tests") == "code-review-tests"
    repo = _project_repo(tmp_path)

    for prompt_id in ("code-review-base", "code-review-verify", "code-review-tests"):
        assert eval_solver._code_review_prompt_id(prompt_id, repo_root=str(repo)) == prompt_id


def test_eval_calibrate_plan_review_with_repo_root_keeps_existing_arm(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    _write_eval_spec(repo, "plan-review-F1")
    assert "F1" not in code_review_registry.effective_criteria(str(repo))
    calls: list[str] = []

    def solve(prompt_id: str, case: dict) -> dict:
        calls.append(prompt_id)
        return {"findings": [{"finding": case["id"]}]}

    result = _eval.calibrate_criterion("F1", repo_root=str(repo), solve=solve)

    assert result["prompt"] == "plan-review-F1"
    assert calls == ["plan-review-F1"]


def test_eval_calibrate_missing_project_spec_has_logical_id_and_path(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    assert _PROJECT_ID in code_review_registry.effective_criteria(str(repo))

    with pytest.raises(_eval.EvalError) as exc_info:
        _eval.calibrate_criterion(
            _PROJECT_ID,
            repo_root=str(repo),
            solve=lambda _prompt_id, _case: {"findings": []},
        )

    message = str(exc_info.value)
    assert _PROJECT_ID in message
    assert f"{_PROJECT_PROMPT}.eval.yaml" in message


def test_cli_admits_code_review_only_project_criterion(tmp_path, monkeypatch, capsys) -> None:
    repo = _project_repo(tmp_path)
    from rebar import config
    from rebar._cli._llm_commands import _criteria
    from rebar.llm.plan_review import registry as plan_review_registry

    assert _PROJECT_ID not in plan_review_registry.by_id(str(repo))
    assert _PROJECT_ID in code_review_registry.effective_criteria(str(repo))
    monkeypatch.setattr(config, "repo_root", lambda: repo)
    calls: list[tuple[str, str | None, int]] = []

    def calibrate(criterion_id: str, *, repo_root=None, runs: int = 1) -> dict:
        calls.append((criterion_id, repo_root, runs))
        return {"criterion": criterion_id, "prompt": _PROJECT_PROMPT}

    monkeypatch.setattr(_eval, "calibrate_criterion", calibrate)

    assert _criteria(["eval", _PROJECT_ID, "--output", "json"]) == 0
    assert calls == [(_PROJECT_ID, str(repo), 1)]
    assert json.loads(capsys.readouterr().out)["prompt"] == _PROJECT_PROMPT


def test_cli_rejects_unknown_criterion_with_distinct_message(tmp_path, monkeypatch, capsys) -> None:
    repo = _project_repo(tmp_path)
    from rebar import config
    from rebar._cli._llm_commands import _criteria

    monkeypatch.setattr(config, "repo_root", lambda: repo)

    def unexpected_calibration(*_args, **_kwargs):
        pytest.fail("unknown criteria must be rejected before calibration")

    monkeypatch.setattr(_eval, "calibrate_criterion", unexpected_calibration)

    assert _criteria(["eval", "project.unknown"]) == 1
    error = capsys.readouterr().err
    assert "unknown criterion 'project.unknown'" in error
    assert "ambiguous" not in error


def test_cli_rejects_both_gates_as_ambiguous(tmp_path, monkeypatch, capsys) -> None:
    repo = _project_repo(tmp_path)
    _activate_plan_review_project_criterion(repo)
    from rebar import config
    from rebar._cli._llm_commands import _criteria
    from rebar.llm.plan_review import registry as plan_review_registry

    assert _PROJECT_ID in plan_review_registry.by_id(str(repo))
    assert _PROJECT_ID in code_review_registry.effective_criteria(str(repo))
    monkeypatch.setattr(config, "repo_root", lambda: repo)

    def unexpected_calibration(*_args, **_kwargs):
        pytest.fail("ambiguous criteria must be rejected before calibration")

    monkeypatch.setattr(_eval, "calibrate_criterion", unexpected_calibration)

    assert _criteria(["eval", _PROJECT_ID]) == 1
    error = capsys.readouterr().err
    assert f"ambiguous criterion '{_PROJECT_ID}'" in error
    assert "plan_review" in error
    assert "code_review" in error


def test_cli_help_names_project_code_review_example(capsys) -> None:
    from rebar._cli._llm_commands import _criteria

    with pytest.raises(SystemExit) as exc_info:
        _criteria(["eval", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert _PROJECT_ID in help_text
    assert "code-review" in help_text


def test_project_criterion_fan_in_executes_once_through_production_two_round_gate(
    tmp_path, monkeypatch
) -> None:
    repo = _project_repo(tmp_path)
    runner = _ProductionRecordingRunner()
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(detectors, "run_security_detectors", lambda **kwargs: {})

    verdict = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="local",
            diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('changed')\n",
            changed_files=["x.py"],
            runner=runner,
            repo_root=str(repo),
            enabled=True,
        )
    )

    assert verdict["verdict"] == "PASS"
    assert runner.calls.count(_PROJECT_PROMPT) == 1
    assert "code-review-tests" in runner.calls
    project_request = next(req for req in runner.requests if req.reviewers == [_PROJECT_PROMPT])
    assert "Find project foo violations in the supplied change." in (project_request.system_prompt)
    findings = verdict["advisory"] + verdict["blocking"]
    project = next(f for f in findings if f.get("reviewer_id") == _PROJECT_PROMPT)
    assert project["criteria"] == ["existing-tag", _PROJECT_ID]


def test_project_criteria_overlay_absent_preserves_batch_parity(tmp_path) -> None:
    agent = _RecordingBatchAgent()
    request = BatchRunRequest(
        finder="code-review-base",
        criteria=({"prompt": "code-review-tests"},),
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=str(tmp_path),
        run_id="parity",
        step_id="round_a",
    )

    result = CodeReviewBatchRunner(context="DIFF").run(request, agent)

    assert agent.calls == [("round_a:code-review-tests", "code-review-tests")]
    assert result.outputs["criteria_count"] == 1
    assert result.outputs["batch_plan"]["ran"] == ["code-review-tests"]
    assert result.outputs["findings"][0]["criteria"] == ["existing-tag"]


def test_project_det_criterion_stays_outside_llm_fan_in(tmp_path, monkeypatch) -> None:
    repo = _project_repo(
        tmp_path,
        with_prompt=False,
        routing={
            **_PROJECT_ROUTING,
            "exec": "DET",
            "detector": {"id": "project.foo"},
        },
    )
    runner = _ProductionRecordingRunner()
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(detectors, "run_security_detectors", lambda **kwargs: {})

    verdict = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="local",
            diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('changed')\n",
            changed_files=["x.py"],
            runner=runner,
            repo_root=str(repo),
            enabled=True,
        )
    )

    assert verdict["verdict"] == "PASS"
    assert _PROJECT_PROMPT not in runner.calls


def _project_finding() -> dict:
    return {
        "finding": "project routing finding",
        "criteria": [_PROJECT_ID],
        "evidence": ["x.py:1"],
        "location": "x.py:1",
    }


def _project_verification() -> dict:
    return {
        "index": 0,
        "binary": {
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
        "severity_attributes": {},
    }


def _decide_project_finding(repo_root: str | None) -> dict:
    return _ex.STEP_REGISTRY["code_review_decide"](
        _ex.StepContext(
            run_id="project-routing",
            step_id="decide",
            kind="uses",
            step={"uses": "code_review_decide"},
            inputs={
                "findings": [_project_finding()],
                "verifications": [_project_verification()],
            },
            workflow={},
            repo_root=repo_root,
        )
    )


def test_project_routing_controls_pass3_blocking(tmp_path) -> None:
    repo = _project_repo(
        tmp_path,
        routing={
            **_PROJECT_ROUTING,
            "block_threshold": 0.0,
            "blocking_enabled": True,
        },
    )

    result = _decide_project_finding(str(repo))

    assert [finding["finding"] for finding in result["blocking"]] == ["project routing finding"]
    assert not result["surfaced"]


def test_project_routing_controls_pass3_nit_suppression(tmp_path) -> None:
    repo = _project_repo(
        tmp_path,
        routing={
            **_PROJECT_ROUTING,
            "blocking_enabled": False,
            "nit_suppressed": True,
        },
    )

    result = _decide_project_finding(str(repo))

    assert not result["surfaced"]
    assert result["dropped"][0]["decision"] == "dropped"
    assert result["dropped"][0]["reason"] == "nit-suppressed"


def test_project_routing_controls_pass3_through_production_gate(tmp_path, monkeypatch) -> None:
    repo = _project_repo(
        tmp_path,
        routing={
            **_PROJECT_ROUTING,
            "block_threshold": 0.0,
            "blocking_enabled": True,
        },
    )
    runner = _ProductionRecordingRunner()
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(detectors, "run_security_detectors", lambda **kwargs: {})

    verdict = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="local",
            diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('changed')\n",
            changed_files=["x.py"],
            runner=runner,
            repo_root=str(repo),
            enabled=True,
        )
    )

    project = next(
        finding for finding in verdict["blocking"] if finding.get("reviewer_id") == _PROJECT_PROMPT
    )
    assert project["criteria"] == ["existing-tag", _PROJECT_ID]


def test_pass3_routing_parity_overlay_absent_repo_preserves_disposition(
    tmp_path,
) -> None:
    repo = _project_repo(tmp_path)
    (repo / ".rebar" / "criteria_routing.json").unlink()

    repo_aware = _decide_project_finding(str(repo))

    assert not repo_aware["blocking"]
    assert not repo_aware["dropped"]
    assert len(repo_aware["surfaced"]) == 1
    finding = repo_aware["surfaced"][0]
    assert finding["decision"] == "advisory"
    assert finding["reason"] == "default-advisory"
    assert finding["block_threshold"] == 0.95
    assert finding["blocking_enabled"] is False


# ── `applies_to` gates a project criterion on the changed files (bug d343-47c6) ─────────
# A project code-review criterion's `applies_to` used to be stored and never read, so the
# criterion ran on EVERY review regardless of what changed. These pin the gate at the real
# dispatch seam (`produce_code_review_verdict`), not at a helper.
def _verdict_with_changed_files(
    repo: Path,
    runner: _ProductionRecordingRunner,
    monkeypatch,
    changed_files: list[str],
) -> dict:
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(detectors, "run_security_detectors", lambda **kwargs: {})
    return gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(repo_root=str(repo)),
            head="HEAD",
            source="local",
            diff_text="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('changed')\n",
            changed_files=changed_files,
            runner=runner,
            repo_root=str(repo),
            enabled=True,
        )
    )


def test_project_criterion_glob_matching_a_changed_file_is_dispatched(
    tmp_path, monkeypatch
) -> None:
    repo = _project_repo(tmp_path, routing={**_PROJECT_ROUTING, "applies_to": ["**/*.py"]})
    runner = _ProductionRecordingRunner()

    verdict = _verdict_with_changed_files(repo, runner, monkeypatch, ["x.py"])

    assert verdict["verdict"] == "PASS"
    assert runner.calls.count(_PROJECT_PROMPT) == 1


def test_project_criterion_glob_matching_no_changed_file_is_not_dispatched(
    tmp_path, monkeypatch
) -> None:
    repo = _project_repo(
        tmp_path, routing={**_PROJECT_ROUTING, "applies_to": ["**/.github/workflows/**"]}
    )
    runner = _ProductionRecordingRunner()

    verdict = _verdict_with_changed_files(repo, runner, monkeypatch, ["x.py"])

    assert verdict["verdict"] == "PASS"
    assert _PROJECT_PROMPT not in runner.calls


def test_project_criterion_with_empty_applies_to_is_always_dispatched(
    tmp_path, monkeypatch
) -> None:
    """An EMPTY `applies_to` means UNGATED for a project criterion — the opposite of its
    built-in-overlay meaning (empty = escalation-only, never glob-fires). This is the
    shape `.rebar/criteria_routing.json` uses today, so it must keep running on any diff."""
    repo = _project_repo(tmp_path)  # _PROJECT_ROUTING carries "applies_to": []
    runner = _ProductionRecordingRunner()

    verdict = _verdict_with_changed_files(repo, runner, monkeypatch, ["unrelated/thing.txt"])

    assert verdict["verdict"] == "PASS"
    assert runner.calls.count(_PROJECT_PROMPT) == 1


@pytest.mark.parametrize(
    ("applies_to", "changed_files", "expected"),
    [
        ([], ["anything.txt"], True),
        (["**/*.py"], ["src/a.py"], True),
        (["**/*.py"], ["README.md"], False),
        (["**/*.py"], [], False),
        (["docs/**", "**/*.py"], ["docs/guide.md"], True),
    ],
)
def test_activated_project_criteria_honours_applies_to(
    tmp_path, applies_to, changed_files, expected
) -> None:
    repo = _project_repo(tmp_path, routing={**_PROJECT_ROUTING, "applies_to": applies_to})

    activated = gate_dispatch._activated_code_review_project_criteria(str(repo), changed_files)

    assert (_PROJECT_ID in [entry["criterion_id"] for entry in activated]) is expected


def test_project_applies_to_globs_read_the_overlay_not_the_packaged_index(tmp_path) -> None:
    """The globs must resolve through `effective_routing(repo_root)`. The packaged
    `routing_index()` has no entry for a `project.` id at all, so `applies_to_globs`
    (which reads it) would report an EMPTY list and silently fail open."""
    repo = _project_repo(tmp_path, routing={**_PROJECT_ROUTING, "applies_to": ["**/*.py"]})

    assert code_review_registry.applies_to_globs(_PROJECT_ID) == []  # packaged index: absent
    assert code_review_registry.project_applies_to_globs(_PROJECT_ID, str(repo)) == ["**/*.py"]
    assert code_review_registry.project_criterion_applies(_PROJECT_ID, ["a.py"], str(repo))
    assert not code_review_registry.project_criterion_applies(_PROJECT_ID, ["a.md"], str(repo))
