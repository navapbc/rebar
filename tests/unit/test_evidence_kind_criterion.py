"""Contract tests for the built-in ``evidence-kind`` plan-review criterion.

The criterion closes the ADR-0043 trust bypass without turning repository-adjacent
operational outcomes into code facts.  Runtime classification quality is covered by the
frozen live eval; these tests pin its registration, blocking path, prompt contract, corpus,
guide, and unchanged completion-tag semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.criteria.model import threshold_for
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.plan_review import orchestrator, registry
from rebar.llm.plan_review.decide_ops import (
    enrich_operator_attested,
    operator_attested_ac_texts,
)
from rebar.llm.prompting import prompts
from rebar.llm.review_kernel import GRADED_BINARY

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CID = "evidence-kind"
PROMPT_ID = "plan-review-evidence-kind"
PROMPT = ROOT / "src/rebar/llm/reviewers/plan_review_evidence_kind.md"
ROUTING = ROOT / "src/rebar/llm/plan_review/criteria_routing.json"
EVAL = ROOT / "src/rebar/llm/eval_specs/plan-review-evidence-kind.eval.yaml"


def _routing_entry() -> dict:
    return json.loads(ROUTING.read_text(encoding="utf-8"))[CID]


def _prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _eval_spec() -> dict:
    return yaml.safe_load(EVAL.read_text(encoding="utf-8"))


def test_registered_as_canonical_agent_code_grounded() -> None:
    assert CID in registry.CANONICAL_LLM
    assert CID in registry.AGENT_TIER
    assert CID in registry.CODEBASE_GROUNDED
    assert registry.validate_packaged_routing() == []


def test_routes_on_every_work_plan_at_blocking_high_confidence_posture() -> None:
    entry = _routing_entry()
    assert entry["exec"] == "AGENT"
    assert entry["facet"] == "codebase-grounding"
    assert entry["applies_at"]["scope"] == ["container", "leaf"]
    assert "trigger" not in entry
    assert entry["default_posture"] == "blocking"
    assert entry["block_threshold"] == 0.95
    threshold, blocking = threshold_for([CID], registry.by_id(), gate="plan_review")
    assert (threshold, blocking) == (0.95, True)


def test_high_confidence_finding_blocks_through_normal_pass3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator.review_kernel, "impact_plan", lambda attrs: 0.97)
    finding = {
        "criteria": [CID],
        "finding": "A repository fact is incorrectly tagged operator-attested.",
    }
    all_yes = {question: "yes" for question in GRADED_BINARY}
    verifications = {0: {"binary": all_yes, "severity_attributes": {}}}
    decided = orchestrator.pass3_over_findings([finding], verifications)[0]
    assert decided["priority"] == 0.97
    assert decided["decision"] == "block"
    assert decided["blocking_enabled"] is True


def test_prompt_is_agentic_and_resolves_through_production_loader() -> None:
    assert criterion_prompt_id(CID) == PROMPT_ID
    loaded = prompts.get_prompt(PROMPT_ID, repo_root=str(ROOT))
    assert loaded.execution_mode == "agentic"
    assert loaded.dimension == "codebase-grounding"
    assert loaded.category == "plan-review-criterion"
    assert loaded.title and loaded.title != CID


@pytest.mark.parametrize(
    "required",
    [
        "every acceptance-criterion checkbox",
        "affirmative repository evidence",
        "exact file path",
        "symbol",
        "ticket comments cannot substitute for repository proof",
        "[operator-attested]",
        "[operator_attested]",
        "split-required",
        "test run",
        "AWS",
        "database",
        "abstain",
    ],
)
def test_prompt_pins_classification_and_anti_false_positive_contract(required: str) -> None:
    assert required.casefold() in _prompt_text().casefold()


def test_prompt_distinguishes_related_code_from_external_outcome_evidence() -> None:
    body = " ".join(_prompt_text().casefold().split())
    assert "existence of test code" in body
    assert "does not make a completed test run" in body
    assert "existence of deployment" in body
    assert "does not make a deployment outcome" in body
    assert "existence of migration" in body
    assert "does not make a database mutation" in body


def test_exact_tag_semantics_and_pass3_enrichment_remain_fail_safe() -> None:
    exact = "## Acceptance Criteria\n- [ ] [Operator-Attested] AWS deployment completed\n"
    malformed = "## Acceptance Criteria\n- [ ] [operator_attested] AWS deployment completed\n"
    assert operator_attested_ac_texts(exact) == ["AWS deployment completed"]
    assert operator_attested_ac_texts(malformed) == []

    finding = {
        "location": "Acceptance Criteria",
        "finding": "AWS deployment completed is contradicted by repository evidence",
        "criteria": [CID],
    }
    verification = {
        0: {
            "severity_attributes": {
                "ac_unverifiable": "missing_oracle",
                "divergent_implementation": "contradicts_reality",
            }
        }
    }
    enrich_operator_attested([finding], verification, exact)
    attrs = verification[0]["severity_attributes"]
    assert attrs["ac_unverifiable"] == "none"
    assert attrs["divergent_implementation"] == "contradicts_reality"


def test_frozen_multisample_eval_is_valid_and_pins_quality_thresholds() -> None:
    spec = _eval_spec()
    assert spec["prompt"] == PROMPT_ID
    assert spec["model"] == "bedrock:us.anthropic.claude-sonnet-4-6"
    assert spec["epochs"] >= 2
    assert spec["gate"] == f"at_least({spec['epochs']})"
    assert spec["coverage_threshold"] == 1.0
    assert validate_eval_spec(spec, strict=True) == []
    assert spec["quality_thresholds"] == {
        "genuine_external_precision": 1.0,
        "named_fixture_recall": 1.0,
        "macro_precision": 0.9,
        "macro_recall": 0.9,
        "previously_passing_regressions": 0,
    }


def test_eval_corpus_covers_named_misses_bypass_controls_and_mixed_case() -> None:
    dataset = {case["id"]: case for case in _eval_spec()["dataset"]}
    required_findings = {
        "EK-F-A1",
        "EK-F-115B",
        "EK-F-8C4F",
        "EK-F-MALFORMED",
        "EK-F-OVERTAG-METHOD",
        "EK-F-OVERTAG-CONFIG",
        "EK-F-MIXED-SPLIT",
    }
    genuine_external = {
        "EK-P-TEST-RUN",
        "EK-P-AWS-DEPLOY",
        "EK-P-DB-MODIFIED",
    }
    clean_codebase = {"EK-P-CODE-UNTAGGED"}
    assert required_findings <= dataset.keys()
    assert genuine_external | clean_codebase <= dataset.keys()
    assert all(dataset[case_id]["expect"] == "finding" for case_id in required_findings)
    assert all(dataset[case_id]["expect"] == "pass" for case_id in genuine_external)
    assert all(dataset[case_id]["expect"] == "pass" for case_id in clean_codebase)
    assert all(dataset[case_id]["regression_control"] for case_id in genuine_external)


def test_guide_and_adr_are_in_sync_with_the_new_prevention() -> None:
    assert registry.validate_criteria_guide(str(ROOT)) == []
    guide = (ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {CID}" in guide
    adr = (ROOT / "docs/adr/0043-operator-attested-completion-evidence.md").read_text(
        encoding="utf-8"
    )
    assert "Plan-time evidence-kind validation" in adr
    assert "ticket comments cannot substitute for repository proof" in adr
