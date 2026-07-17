"""Regression: the live eval path resolves namespaced project criteria (bug 2340).

`rebar criteria eval <project.criterion>` / `calibrate_criterion` computes the physical
prompt id `plan-review-project-<name>` (the forward map replaces the `project.` dot with a
dash) to load the eval fixture, then runs the criterion via `eval_solver.run_case`. The
reverse resolution (`_criterion_id`) stripped only the `plan-review-` prefix, leaving
`project-<name>` (dash), which never matched the dot-keyed registry entry `project.<name>`
— so every namespaced project criterion failed with `no eval solver`.

These tests are self-contained: they build their own tmp project-criterion overlay and do
NOT depend on any repository `.rebar/` configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rebar.llm.evals import eval as _eval
from rebar.llm.evals import eval_solver
from rebar.llm.prompting import prompt_library
from rebar.llm.runner import FakeRunner

_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}
_RUBRIC = (
    "---\n"
    "schema_version: 1\n"
    "title: Test crit\n"
    "description: a self-contained project criterion for the 2340 regression\n"
    "execution_mode: single_turn\n"
    "category: plan-review-criterion\n"
    "dimension: project-invariants\n"
    "---\n"
    "Flag the thing.\n"
)
_EVAL = {
    "prompt": "plan-review-project-testcrit",
    "model": "anthropic:claude-sonnet-4-6",
    "epochs": 1,
    "gate": "at_least(1)",
    "coverage_threshold": 1.0,
    "scorers": [{"type": "deterministic", "name": "emits_valid_findings", "description": "d"}],
    "dataset": [
        {"id": "T-F", "expect": "finding", "input": "a plan that should fire"},
        {"id": "T-N", "expect": "pass", "input": "a plan that should not fire"},
    ],
    "gold_set": [
        {"input": "fires", "label": "finding"},
        {"input": "passes", "label": "pass"},
    ],
}


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


def _make_repo(tmp_path: Path) -> str:
    rebar_dir = tmp_path / ".rebar"
    (rebar_dir / "prompts").mkdir(parents=True)
    (rebar_dir / "evals").mkdir(parents=True)
    overlay = {"plan_review": {"project.testcrit": _ROUTING}, "activate": ["project.testcrit"]}
    (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    prompt_md = rebar_dir / "prompts" / "plan-review-project-testcrit.md"
    prompt_md.write_text(_RUBRIC, encoding="utf-8")
    (rebar_dir / "evals" / "plan-review-project-testcrit.eval.yaml").write_text(
        yaml.safe_dump(_EVAL), encoding="utf-8"
    )
    return str(tmp_path)


def _fake() -> FakeRunner:
    finding = {"finding": "x", "criteria": ["project.testcrit"]}
    return FakeRunner(structured={"analysis": "", "findings": [finding]})


# ── the reverse id map recovers the dotted logical id ────────────────────────────
def test_criterion_id_resolves_namespaced_project(tmp_path):
    root = _make_repo(tmp_path)
    assert eval_solver._criterion_id("plan-review-project-testcrit", root) == "project.testcrit"


# ── contrast: built-ins + unknown ids are unaffected ─────────────────────────────
def test_criterion_id_builtin_and_unknown_unchanged():
    assert eval_solver._criterion_id("plan-review-F1", None) == "F1"
    assert eval_solver._criterion_id("plan-review-does-not-exist", None) is None
    assert eval_solver._criterion_id("code-review-security", None) is None


# ── run_case resolves + runs the project criterion (no "no eval solver") ─────────
def test_run_case_resolves_namespaced_project(tmp_path):
    root = _make_repo(tmp_path)
    out = eval_solver.run_case(
        "plan-review-project-testcrit", {"input": "a plan text"}, runner=_fake(), repo_root=root
    )
    assert out.get("findings"), "the resolved criterion should have produced the fake's finding"


# ── the full calibrate_criterion CLI path resolves + runs over the dataset ───────
def test_calibrate_resolves_namespaced_project(tmp_path):
    root = _make_repo(tmp_path)
    r = _eval.calibrate_criterion("project.testcrit", repo_root=root, runner=_fake(), runs=1)
    assert r["prompt"] == "plan-review-project-testcrit"
    assert (r["n_fire"], r["n_nofire"]) == (1, 1)
    # the fake fires on the must-fire case → the criterion actually ran (recall computed)
    assert r["recall"] == 1.0
