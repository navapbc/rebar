"""Offline contract tests for the project ci-gate-compat criterion.

The criterion answers a CI-agnostic question — does a pipeline/gate-configuration change add a
hard-fail step with no if-present/grandfather guard, so a branch created before the change would
fail? — and it is TRIGGERED declaratively: the routing entry's ``applies_to`` globs (overridable
by ``[code_review] ci_config_globs``) decide whether it joins the Round-A fan-in. No shipped
module names a CI vendor, so adding a CI system is adding a glob, not editing code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from rebar.llm.code_review import registry as code_review_registry
from rebar.llm.criteria import overlay as criteria_overlay
from rebar.llm.criteria.model import CriteriaError
from rebar.llm.evals import eval as _eval
from rebar.llm.prompting import prompts
from rebar.llm.workflow import gate_dispatch

REPO_ROOT = str(Path(__file__).resolve().parents[2])
CRITERION_ID = "project.ci-gate-compat"
PROMPT_ID = "code-review-project-ci-gate-compat"
_REBAR = Path(REPO_ROOT) / ".rebar"
_PROMPT_FILE = _REBAR / "prompts" / f"{PROMPT_ID}.md"
_EVAL_FILE = _REBAR / "evals" / f"{PROMPT_ID}.eval.yaml"
_FIRE_IDS = {f"CG-F{i}" for i in range(1, 7)}
_PASS_IDS = {f"CG-N{i}" for i in range(1, 7)}
_CONFIG_KEY = "code_review.ci_config_globs"
_CI_GLOBS = [
    "**/.github/workflows/**",
    "**/Jenkinsfile*",
    "**/.gitlab-ci.yml",
    "**/.circleci/**",
    "**/azure-pipelines*.y*ml",
    "**/.buildkite/**",
    "**/bitbucket-pipelines.yml",
    "**/.drone.yml",
]
# Every vendor name that may appear in the DECLARATIVE glob set but never in the rubric prose.
_VENDOR_WORDS = ("github", "gitlab", "jenkins", "circleci", "buildkite", "bitbucket", "drone")


def _spec() -> dict:
    return _eval.load_eval_spec(PROMPT_ID, repo_root=REPO_ROOT)


# ── The criterion's project-owned assets ──────────────────────────────────────────────────


def test_routing_entry_is_advisory_and_glob_triggered() -> None:
    routing_doc = json.loads((_REBAR / "criteria_routing.json").read_text(encoding="utf-8"))
    routing = routing_doc["code_review"][CRITERION_ID]

    assert CRITERION_ID in routing_doc["activate"]
    assert routing_doc["activate"][CRITERION_ID] == ["code_review"]
    assert routing["exec"] == "1-TURN"
    assert routing["default_posture"] == "advisory"
    assert routing["blocking_enabled"] is False
    assert routing["block_threshold"] == 0.90
    assert routing["applies_to"] == _CI_GLOBS
    assert routing["applies_to_config_key"] == _CONFIG_KEY
    assert CRITERION_ID in code_review_registry.effective_criteria(REPO_ROOT)


def test_rubric_is_ci_agnostic_and_emits_code_review_findings() -> None:
    prompt = prompts.get_prompt(PROMPT_ID, repo_root=REPO_ROOT)

    assert prompt.execution_mode == "single_turn"
    assert prompt.outputs == "code_review_findings"
    assert prompt.category == "code-review-pass"
    assert prompt.dimension == "ci-gate-compat"

    body = prompt.text.lower()
    for vendor in _VENDOR_WORDS:
        assert vendor not in body, f"rubric must stay CI-agnostic; found {vendor!r}"
    # The T8 coaching from the plan review: the recognition vocabulary is DEFINED in-body.
    for marker in (
        "hard-fail",
        "guard",
        "grandfather",
        "do not flag",
        "abstain",
        "pipeline",
    ):
        assert marker in body


def test_eval_spec_is_strict_balanced_and_uses_code_review_schema() -> None:
    assert _eval.eval_spec_path(PROMPT_ID, repo_root=REPO_ROOT) == _EVAL_FILE
    spec = _spec()

    assert _eval.validate_eval_spec(spec, strict=True) == []
    assert spec["prompt"] == PROMPT_ID
    assert spec["coverage_threshold"] == 1.0

    dataset = spec["dataset"]
    assert len(dataset) == 12
    by_id = {case["id"]: case for case in dataset}
    assert set(by_id) == _FIRE_IDS | _PASS_IDS
    assert {cid for cid, case in by_id.items() if case["expect"] == "finding"} == _FIRE_IDS
    assert {cid for cid, case in by_id.items() if case["expect"] == "pass"} == _PASS_IDS
    for case in dataset:
        assert isinstance(case.get("diff"), str) and case["diff"].startswith("--- ")

    gold = spec["gold_set"]
    assert [entry["label"] for entry in gold].count("finding") == 6
    assert [entry["label"] for entry in gold].count("pass") == 6


def test_injected_perfect_solver_meets_calibration_metric_contract() -> None:
    def perfect_solve(_prompt_id, case):
        fire = case["expect"] == "finding"
        findings = (
            [
                {
                    "finding": "ungrandfathered hard-fail gate",
                    "criteria": [CRITERION_ID],
                    "evidence": ["pipeline:1"],
                    "location": "pipeline:1",
                    "suggested_fix": "",
                }
            ]
            if fire
            else []
        )
        return {"findings": findings}

    report = _eval.calibrate_criterion(
        CRITERION_ID, repo_root=REPO_ROOT, solve=perfect_solve, runs=3
    )

    assert report["prompt"] == PROMPT_ID
    assert (report["n_fire"], report["n_nofire"]) == (6, 6)
    assert report["recall"] == 1.0
    assert report["false_accept"] == 0.0


def test_project_assets_are_committed_and_documented() -> None:
    ignore = (Path(REPO_ROOT) / ".gitignore").read_text(encoding="utf-8")
    assert f"!{_PROMPT_FILE.relative_to(REPO_ROOT)}" in ignore
    assert f"!{_EVAL_FILE.relative_to(REPO_ROOT)}" in ignore

    docs = (Path(REPO_ROOT) / "docs" / "llm-framework.md").read_text(encoding="utf-8")
    section = docs.split("Project dogfood: ci-gate-compat", 1)
    assert len(section) == 2
    body = section[1][:4000].lower()
    for marker in ("advisory", "applies_to", "ci_config_globs", "cg-f1", "cg-n6"):
        assert marker in body

    config_docs = (Path(REPO_ROOT) / "docs" / "config.md").read_text(encoding="utf-8")
    assert "ci_config_globs" in config_docs

    golden = json.loads(
        (Path(REPO_ROOT) / "tests" / "golden" / "config_surface.json").read_text(encoding="utf-8")
    )
    assert _CONFIG_KEY in golden["config_keys"]
    assert "REBAR_CODE_REVIEW_CI_CONFIG_GLOBS" in golden["canonical_env_vars"]


# ── The declarative glob trigger (shipped seam) ───────────────────────────────────────────


def _write_overlay(root: Path, *, routing: dict, config_globs: list[str] | None = None) -> None:
    rebar_dir = root / ".rebar"
    (rebar_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (rebar_dir / "criteria_routing.json").write_text(
        json.dumps({"code_review": routing, "activate": {c: ["code_review"] for c in routing}}),
        encoding="utf-8",
    )
    for criterion_id in routing:
        prompt_id = f"code-review-{criterion_id.replace('.', '-')}"
        (rebar_dir / "prompts" / f"{prompt_id}.md").write_text(
            "---\nschema_version: 1\ntitle: t\ndescription: d\n"
            "outputs: code_review_findings\nexecution_mode: single_turn\n"
            "category: code-review-pass\ndimension: d\n---\nbody\n",
            encoding="utf-8",
        )
    if config_globs is not None:
        (root / "rebar.toml").write_text(
            "[code_review]\nci_config_globs = " + json.dumps(config_globs) + "\n",
            encoding="utf-8",
        )


_GATED = {
    "project.gated": {
        "exec": "1-TURN",
        "applies_to": ["**/.github/workflows/**", "**/Jenkinsfile*"],
        "applies_to_config_key": _CONFIG_KEY,
        "default_posture": "advisory",
        "block_threshold": 0.9,
        "blocking_enabled": False,
    },
    "project.always": {
        "exec": "1-TURN",
        "applies_to": [],
        "default_posture": "advisory",
        "block_threshold": 0.9,
        "blocking_enabled": False,
    },
}


def _activated(root: Path, changed_files) -> set[str]:
    return {
        entry["criterion_id"]
        for entry in gate_dispatch._activated_code_review_project_criteria(str(root), changed_files)
    }


@pytest.mark.parametrize(
    ("changed_files", "expected"),
    [
        (["src/rebar/x.py", ".github/workflows/ci.yml"], {"project.gated", "project.always"}),
        (["Jenkinsfile"], {"project.gated", "project.always"}),
        (["src/rebar/x.py"], {"project.always"}),
        ([], {"project.always"}),
    ],
)
def test_glob_trigger_gates_project_criteria_but_never_the_ungated_ones(
    tmp_path, changed_files, expected
) -> None:
    _write_overlay(tmp_path, routing=_GATED)

    assert _activated(tmp_path, changed_files) == expected


def test_config_key_replaces_the_declared_globs(tmp_path) -> None:
    _write_overlay(tmp_path, routing=_GATED, config_globs=["ci/pipeline.yaml"])

    assert _activated(tmp_path, ["ci/pipeline.yaml"]) == {"project.gated", "project.always"}
    assert _activated(tmp_path, [".github/workflows/ci.yml"]) == {"project.always"}


def test_effective_globs_fall_back_to_routing_when_config_key_unset(tmp_path) -> None:
    _write_overlay(tmp_path, routing=_GATED)

    assert code_review_registry.project_applies_to_globs("project.gated", str(tmp_path)) == [
        "**/.github/workflows/**",
        "**/Jenkinsfile*",
    ]
    assert code_review_registry.project_applies_to_globs("project.always", str(tmp_path)) == []


@pytest.mark.parametrize("pointer", [5, "", "nodot", "too.many.dots"])
def test_malformed_applies_to_config_key_is_a_located_load_error(pointer) -> None:
    """A typo'd pointer must fail loudly at load, not silently no-op into the routing default."""
    with pytest.raises(CriteriaError, match="applies_to_config_key"):
        criteria_overlay._validate_routing_entry(
            CRITERION_ID, {"applies_to_config_key": pointer}, where="overlay"
        )


def test_empty_config_override_falls_back_to_the_declared_globs(tmp_path) -> None:
    """Config key present but resolving to `[]` is "unset", not "gate on nothing"."""
    _write_overlay(tmp_path, routing=_GATED, config_globs=[])

    assert code_review_registry.project_applies_to_globs("project.gated", str(tmp_path)) == [
        "**/.github/workflows/**",
        "**/Jenkinsfile*",
    ]
    assert _activated(tmp_path, [".github/workflows/ci.yml"]) == {
        "project.gated",
        "project.always",
    }
    assert _activated(tmp_path, ["src/rebar/x.py"]) == {"project.always"}


def test_unreadable_config_fails_open_to_the_declared_globs(tmp_path, caplog) -> None:
    """A malformed value for the pointed-at key must not delete review coverage."""
    _write_overlay(tmp_path, routing=_GATED)
    # `ci_config_globs` coerces via `_as_str_list`, so a scalar is a load-time ConfigError.
    (tmp_path / "rebar.toml").write_text("[code_review]\nci_config_globs = 5\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="rebar.llm.code_review.registry"):
        globs = code_review_registry.project_applies_to_globs("project.gated", str(tmp_path))

    assert globs == ["**/.github/workflows/**", "**/Jenkinsfile*"]
    assert any("unreadable" in r.message for r in caplog.records)
    assert _activated(tmp_path, [".github/workflows/ci.yml"]) == {
        "project.gated",
        "project.always",
    }


def test_config_pointer_naming_no_such_key_fails_open_and_warns(tmp_path, caplog) -> None:
    """A pointer whose `<section>.<key>` does not exist is a typo, reported not swallowed."""
    routing = {
        "project.gated": dict(_GATED["project.gated"], applies_to_config_key="code_review.nope")
    }
    _write_overlay(tmp_path, routing=routing)

    with caplog.at_level(logging.WARNING, logger="rebar.llm.code_review.registry"):
        globs = code_review_registry.project_applies_to_globs("project.gated", str(tmp_path))

    assert globs == ["**/.github/workflows/**", "**/Jenkinsfile*"]
    assert any("names no config key" in r.message for r in caplog.records)
