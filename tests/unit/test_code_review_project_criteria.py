"""Behavioral coverage for project-owned code-review criteria."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.config import LLMConfig
from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.criteria.model import CriteriaError
from rebar.llm.errors import LLMError
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import gate_dispatch
from rebar.llm.workflow.runners import BatchRunRequest

_PROJECT_ID = "project.foo"
_PROJECT_PROMPT = "code-review-project-foo"
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
_PROJECT_ROUTING = {
    "exec": "1-TURN",
    "applies_to": [],
    "default_posture": "advisory",
    "block_threshold": 0.8,
    "blocking_enabled": False,
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
    runner = CodeReviewBatchRunner(context="DIFF", project_criteria=_project_entries())

    round_b = runner.run(_batch_request(step_id="round_b", repo_root=str(tmp_path)), agent)
    first = runner.run(_batch_request(step_id="round_a", repo_root=str(tmp_path)), agent)
    second = runner.run(_batch_request(step_id="round_a", repo_root=str(tmp_path)), agent)

    assert round_b.outputs["criteria_count"] == 0
    assert round_b.outputs["batch_plan"]["ran"] == []
    assert [prompt for _, prompt in agent.calls] == [_PROJECT_PROMPT, _PROJECT_PROMPT]
    assert first.outputs == second.outputs


def test_project_criterion_missing_prompt_raises_located_llm_error(tmp_path) -> None:
    runner = CodeReviewBatchRunner(context="DIFF", project_criteria=_project_entries())

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
    runner = CodeReviewBatchRunner(context="DIFF", project_criteria=_project_entries())

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


class _ProductionRecordingRunner:
    name = "project-criteria-test"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def preflight(self) -> None:
        return None

    def run(self, req):
        prompt_id = req.reviewers[0]
        self.calls.append(prompt_id)
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
