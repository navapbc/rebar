"""Offline contract tests for the project review-phase-boundaries criterion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.code_review import registry as code_review_registry
from rebar.llm.evals import eval as _eval
from rebar.llm.evals import eval_solver
from rebar.llm.prompting import prompts
from rebar.llm.runner import FakeRunner

REPO_ROOT = str(Path(__file__).resolve().parents[2])
CRITERION_ID = "project.review-phase-boundaries"
PROMPT_ID = "code-review-project-review-phase-boundaries"
_REBAR = Path(REPO_ROOT) / ".rebar"
_PROMPT_FILE = _REBAR / "prompts" / f"{PROMPT_ID}.md"
_EVAL_FILE = _REBAR / "evals" / f"{PROMPT_ID}.eval.yaml"
_FIRE_IDS = {f"RP-F{i}" for i in range(1, 7)}
_PASS_IDS = {f"RP-N{i}" for i in range(1, 7)}


def _spec() -> dict:
    return _eval.load_eval_spec(PROMPT_ID, repo_root=REPO_ROOT)


def test_project_criterion_assets_form_one_runnable_happy_path() -> None:
    routing_doc = json.loads((_REBAR / "criteria_routing.json").read_text(encoding="utf-8"))
    routing = routing_doc["code_review"][CRITERION_ID]
    assert CRITERION_ID in routing_doc["activate"]
    assert routing["exec"] == "1-TURN"
    assert routing["block_threshold"] == 0.90
    assert routing["blocking_enabled"] is False
    assert routing["default_posture"] == "advisory"
    assert CRITERION_ID in code_review_registry.effective_criteria(REPO_ROOT)

    prompt = prompts.get_prompt(PROMPT_ID, repo_root=REPO_ROOT)
    assert prompt.execution_mode == "single_turn"
    assert prompt.outputs == "code_review_findings"

    spec = _spec()
    assert spec["prompt"] == PROMPT_ID

    finding = {
        "finding": "Pass 1 assigns impact instead of only finding candidates.",
        "criteria": [CRITERION_ID],
        "evidence": ["src/rebar/llm/reviewers/example.md:12"],
        "location": "src/rebar/llm/reviewers/example.md:12",
        "scenarios": ["A candidate is suppressed before independent verification."],
        "suggested_fix": "",
    }

    class _RecordingRunner:
        name = "phase-boundaries-test"

        def __init__(self) -> None:
            self.requests = []

        def preflight(self) -> None:
            return None

        def run(self, req):
            self.requests.append(req)
            return FakeRunner(structured={"findings": [finding]}).run(req)

    runner = _RecordingRunner()
    out = eval_solver.run_case(
        PROMPT_ID,
        {
            "id": "RP-F1",
            "diff": (
                "--- a/reviewer.md\n"
                "+++ b/reviewer.md\n"
                "@@ -1 +1 @@\n"
                "+Pass 1 must assign validity and impact before emitting a candidate.\n"
            ),
        },
        runner=runner,
        repo_root=REPO_ROOT,
    )

    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.mode == "structured"
    assert request.execution_mode == "single_turn"
    assert request.output_schema == "code_review_findings"
    assert (
        "Pass 1 must assign validity and impact before emitting a candidate."
        in request.system_prompt
    )
    assert out["findings"] == [finding]
    assert "severity" not in out["findings"][0]
    assert "confidence" not in out["findings"][0]
    assert out["findings"][0]["suggested_fix"] == ""


def test_prompt_encodes_phase_ownership_and_abstention_guards() -> None:
    prompt = prompts.get_prompt(PROMPT_ID, repo_root=REPO_ROOT)
    assert prompt.category == "code-review-pass"
    assert prompt.dimension == "review-phase-boundaries"
    body = prompt.text.lower()

    for marker in ("pass 1", "pass 2", "pass 3", "pass 4"):
        assert marker in body
    for marker in (
        "grounded evidence",
        "severity",
        "confidence",
        "suggested_fix",
        "documentation",
        "negative example",
        "unchanged",
        "abstain",
        "unclear",
    ):
        assert marker in body


def test_eval_spec_is_strict_balanced_and_uses_code_review_schema() -> None:
    assert _eval.eval_spec_path(PROMPT_ID, repo_root=REPO_ROOT) == _EVAL_FILE
    spec = _spec()
    assert _eval.validate_eval_spec(spec, strict=True) == []
    assert spec["model"] == "anthropic:claude-sonnet-4-6"
    assert spec["epochs"] == 3
    assert spec["gate"] == "at_least(2)"
    assert spec["coverage_threshold"] == 1.0

    deterministic = [s for s in spec["scorers"] if s.get("type") == "deterministic"]
    assert [s["name"] for s in deterministic] == ["code_review_emits_valid_findings"]

    dataset = spec["dataset"]
    assert len(dataset) == 12
    by_id = {case["id"]: case for case in dataset}
    assert set(by_id) == _FIRE_IDS | _PASS_IDS
    assert {cid for cid, case in by_id.items() if case["expect"] == "finding"} == _FIRE_IDS
    assert {cid for cid, case in by_id.items() if case["expect"] == "pass"} == _PASS_IDS
    for case in dataset:
        assert isinstance(case.get("diff"), str) and case["diff"].startswith("--- ")
        assert "input" not in case

    gold = spec["gold_set"]
    assert len(gold) == 12
    assert [entry["label"] for entry in gold].count("finding") == 6
    assert [entry["label"] for entry in gold].count("pass") == 6
    assert all(isinstance(entry.get("input"), str) and entry["input"].strip() for entry in gold)


def test_eval_corpus_covers_each_phase_and_nearest_non_finding_controls() -> None:
    by_id = {case["id"]: case for case in _spec()["dataset"]}

    expected_markers = {
        "RP-F1": ("pass 1", "validity", "impact"),
        "RP-F2": ("pass 1", "block"),
        "RP-F3": ("pass 1", "patch"),
        "RP-F4": ("pass 2", "new"),
        "RP-F5": ("pass 3", "llm"),
        "RP-F6": ("pass 4", "implementation"),
        "RP-N1": ("pass 1", "discover"),
        "RP-N2": ("pass 1", "evidence"),
        "RP-N3": ("pass 2", "yes/no"),
        "RP-N4": ("pass 3", "threshold"),
        "RP-N5": ("pass 4", "investigate"),
        "RP-N6": ("documentation", "suggested_fix"),
    }
    for case_id, markers in expected_markers.items():
        text = f"{by_id[case_id].get('note', '')}\n{by_id[case_id]['diff']}".lower()
        for marker in markers:
            assert marker in text, f"{case_id} must cover {marker!r}"


def test_injected_perfect_solver_meets_calibration_metric_contract() -> None:
    def perfect_solve(_prompt_id, case):
        fire = case["expect"] == "finding"
        findings = (
            [
                {
                    "finding": "phase boundary violation",
                    "criteria": [CRITERION_ID],
                    "evidence": ["x.md:1"],
                    "location": "x.md:1",
                    "suggested_fix": "",
                }
            ]
            if fire
            else []
        )
        return {"findings": findings}

    report = _eval.calibrate_criterion(
        CRITERION_ID,
        repo_root=REPO_ROOT,
        solve=perfect_solve,
        runs=3,
    )

    assert report["prompt"] == PROMPT_ID
    assert report["runs"] == 3
    assert (report["n_fire"], report["n_nofire"]) == (6, 6)
    assert report["recall"] == 1.0
    assert report["false_accept"] == 0.0
    assert report["agreement"] == 1.0
    assert report["kappa"] == pytest.approx(1.0)
    assert report["stability_min"] == pytest.approx(1.0)
    assert report["stability_mean"] == pytest.approx(1.0)


def test_project_assets_are_explicitly_committed_and_documented() -> None:
    ignore = (Path(REPO_ROOT) / ".gitignore").read_text(encoding="utf-8")
    assert f"!{_PROMPT_FILE.relative_to(REPO_ROOT)}" in ignore
    assert f"!{_EVAL_FILE.relative_to(REPO_ROOT)}" in ignore

    docs = (Path(REPO_ROOT) / "docs" / "llm-framework.md").read_text(encoding="utf-8")
    section = docs.split("Project dogfood: review-phase-boundaries", 1)
    assert len(section) == 2
    body = section[1][:4000].lower()
    for marker in (
        "project-owned",
        "pass 1",
        "pass 2",
        "pass 3",
        "pass 4",
        "advisory",
        "rp-f1",
        "rp-n6",
    ):
        assert marker in body


# ── this repo's own criterion stays UNGATED (bug d343-47c6 regression guard) ────────────
def test_repo_criterion_declares_empty_applies_to() -> None:
    routing_doc = json.loads((_REBAR / "criteria_routing.json").read_text(encoding="utf-8"))

    assert routing_doc["code_review"][CRITERION_ID]["applies_to"] == []


@pytest.mark.parametrize(
    "changed_files",
    [
        [],
        ["src/rebar/llm/code_review/registry.py"],
        ["docs/adr/0074-code-review-overlay-escalation.md"],
        ["some/unrelated/asset.png"],
    ],
    ids=["no-files", "python", "docs", "unrelated"],
)
def test_repo_criterion_is_dispatched_whatever_changed(changed_files) -> None:
    """`applies_to: []` means UNGATED for a project criterion, so gating project criteria on
    `applies_to` must leave this repo's only activated code-review criterion always-on."""
    from rebar.llm.workflow import gate_dispatch

    activated = gate_dispatch._activated_code_review_project_criteria(REPO_ROOT, changed_files)

    assert CRITERION_ID in [entry["criterion_id"] for entry in activated]
    assert code_review_registry.project_criterion_applies(CRITERION_ID, changed_files, REPO_ROOT)
