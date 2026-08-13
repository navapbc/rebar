"""Registration tests for the built-in T15 "overlay-derisk" plan-review criterion (story ea28).

Unlike `project.measurement-provenance` (story f161, which rides the `.rebar/` project overlay),
T15 ships in the DEFAULT criteria set — so it changes behaviour for every rebar client and its
registration must be complete and self-consistent.

SCOPE BOUNDARY (from the ticket): this pins the criterion ARTIFACT + REGISTRATION + regenerated
guide. Runtime routing BEHAVIOUR — does T15 actually fire on an infra plan and stay silent on an
app-only plan — is proven by the eval-fixtures story 36ab, which depends on this one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.evals import eval as ev

REPO = Path(__file__).resolve().parents[2]
RUBRIC = REPO / "src/rebar/llm/reviewers/plan_review_T15.md"
ROUTING = REPO / "src/rebar/llm/plan_review/criteria_routing.json"

# The four checks the rubric body must state, plus the S1-S3 applicability gate and the
# anti-false-positive paragraph.
RUBRIC_REQUIRED = (
    "S1",
    "S2",
    "S3",
    "RISK NAMED",
    "FAST OUT-OF-LOOP PROOF",
    "PROVE-THEN-CODIFY",
    "SCOPED CLEANUP",
    "ANTI-FP",
)


def test_rubric_exists() -> None:
    assert RUBRIC.is_file(), f"missing rubric: {RUBRIC}"


@pytest.mark.parametrize("marker", RUBRIC_REQUIRED)
def test_rubric_states_every_gate_and_check(marker: str) -> None:
    assert marker in RUBRIC.read_text(), f"rubric is missing {marker!r}"


def test_rubric_front_matter_is_tool_enabled_and_named() -> None:
    """Two front-matter fields are load-bearing:

    `execution_mode: agentic` — tooling is granted by the PROMPT's execution_mode, and the
    loader enum is single_turn|agentic ("AGENT" is the ROUTING value and is NOT valid here).
    `title:` — build_descriptor computes `name = prompt.title or cid`, so without it the
    criterion's rendered name degrades to the bare id `T15`.
    """
    lines = [ln.strip() for ln in RUBRIC.read_text().splitlines()]
    assert "execution_mode: agentic" in lines, "rubric must be agentic (tool-using)"
    assert "dimension: overlay-derisk" in lines
    title = [ln for ln in lines if ln.startswith("title:")]
    assert title and len(title[0]) > len("title:") + 1, "rubric needs a descriptive title"


def test_routing_entry_has_the_required_values() -> None:
    """The KEY existing is not enough — the values are what route the criterion."""
    entry = json.loads(ROUTING.read_text())["T15"]
    assert entry["exec"] == "AGENT"
    assert entry["facet"] == "overlay-derisk"
    assert entry["overlay_routing"] == "llm", "content-routed by the orchestrator, like T13/T14"
    assert entry["applies_at"]["suppress_types"] == ["bug"]


def _t15_spec() -> dict:
    return ev.load_eval_spec("plan-review-T15")


def _t15_cases() -> dict[str, dict]:
    return {case["id"]: case for case in _t15_spec()["dataset"]}


T15_EXPECTED_CASES = {
    "T15-P-deployed-service-fast-proof": ("pass", "applicable"),
    "T15-F-full-slow-loop-only": ("finding", "applicable"),
    "T15-F-cleanup-drops-preexisting-table": ("finding", "applicable"),
    "T15-F-cleanup-wipes-shared-bucket": ("finding", "applicable"),
    "T15-P-not-applicable-app-only": ("pass", "not-applicable"),
    "T15-P-not-applicable-library-only": ("pass", "not-applicable"),
    "T15-P-not-applicable-cli-only": ("pass", "not-applicable"),
    "T15-P-not-applicable-docs-only": ("pass", "not-applicable"),
    "T15-P-not-applicable-health-route": ("pass", "not-applicable"),
    "T15-P-not-applicable-incidental-deploy-wording": ("pass", "not-applicable"),
    "T15-P-not-applicable-fast-cheap-loop": ("pass", "not-applicable"),
    "T15-P-not-applicable-unit-test-settled": ("pass", "not-applicable"),
}


def test_t15_eval_case_matrix_is_complete_and_labeled() -> None:
    cases = _t15_cases()
    assert set(T15_EXPECTED_CASES) <= set(cases)
    for case_id, (expect, mode) in T15_EXPECTED_CASES.items():
        assert cases[case_id]["expect"] == expect
        assert cases[case_id]["mode"] == mode


def test_t15_deployed_service_fast_proof_fixture_carries_the_full_contract() -> None:
    case = _t15_cases()["T15-P-deployed-service-fast-proof"]
    text = case["input"].lower()
    for risk in ("boot", "authentication", "authorization", "connectivity", "readiness"):
        assert risk in text
    for proof_contract in (
        "out-of-loop",
        "before codifying",
        "minutes",
        "only resources created by this experiment",
    ):
        assert proof_contract in text


def test_t15_applicable_findings_pin_slow_loop_and_destructive_cleanup() -> None:
    cases = _t15_cases()
    slow_loop = cases["T15-F-full-slow-loop-only"]["input"].lower()
    assert "repeat the full pipeline until it works" in slow_loop
    assert "do not run the image locally or perform a direct probe" in slow_loop

    table = cases["T15-F-cleanup-drops-preexisting-table"]["input"].lower()
    assert "pre-existing" in table and "drop" in table

    bucket = cases["T15-F-cleanup-wipes-shared-bucket"]["input"].lower()
    assert "wiping the entire shared bucket" in bucket


def test_t15_not_applicable_cases_pin_s1_s2_s3_false_positive_boundaries() -> None:
    cases = _t15_cases()
    for case_id in (
        "T15-P-not-applicable-app-only",
        "T15-P-not-applicable-library-only",
        "T15-P-not-applicable-cli-only",
        "T15-P-not-applicable-docs-only",
        "T15-P-not-applicable-health-route",
        "T15-P-not-applicable-incidental-deploy-wording",
        "T15-P-not-applicable-fast-cheap-loop",
        "T15-P-not-applicable-unit-test-settled",
    ):
        assert cases[case_id]["expect"] == "pass"
        assert cases[case_id]["mode"] == "not-applicable"


def test_t15_eval_uses_the_standard_deterministic_scorers() -> None:
    scorer_names = {
        scorer["name"] for scorer in _t15_spec()["scorers"] if scorer.get("type") == "deterministic"
    }
    assert scorer_names == {"no_fire_on_good_cases", "recall_on_seeded_defects"}


def test_t15_application_scope_negatives_are_recorded_as_regression_guards() -> None:
    ledger = (
        REPO / "docs/experiments/plan-review-gate/eval/observed-false-positives.md"
    ).read_text()
    normalized = ledger.lower().replace("-", " ")
    assert "t15 application scope regression guards" in normalized
    for guard in ("app only", "library only", "cli only", "docs only", "health route"):
        assert guard in normalized
