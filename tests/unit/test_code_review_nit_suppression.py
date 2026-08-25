"""Impact-aware nit suppression for documentation findings.

Bug 2dfe records why documentation findings with positive impact must remain visible. This module
pins that decision behavior. Documentation rubric contracts live in
``test_code_review_docs_overlay.py``.

Proving command:
    .venv/bin/pytest tests/unit/test_code_review_nit_suppression.py -v
"""

from __future__ import annotations

import pytest

import rebar.llm.workflow.executor as _ex
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops

pytestmark = pytest.mark.unit

_GRADED_YES = {
    "is_verifiable": "yes",
    "evidence_entails_finding": "yes",
    "path_reachable": "yes",
    "impact_follows_necessarily": "yes",
    "no_viable_alternative_explanation": "yes",
    "no_existing_mitigation": "yes",
    "severity_claim_justified": "yes",
}

# contract_drift (maintainability, moderate tier 0.6) with churn90 absent (freq_mult 0.5) and no
# silence amplifier (amp 0.8) → impact_code = 0.6 * 0.5 * 0.8 = 0.24 > 0.
_IMPACTFUL_ATTRS = {"contract_drift": "yes"}


def _ctx(inputs):
    return _ex.StepContext(
        run_id="r",
        step_id="s",
        kind="uses",
        step={"uses": "code_review_decide"},
        inputs=inputs,
        workflow={},
        repo_root=None,
    )


def _decide(findings, verifs):
    return _ex.STEP_REGISTRY["code_review_decide"](
        _ctx({"findings": findings, "verifications": verifs})
    )


def _verif(index, attrs=None):
    return {"index": index, "binary": dict(_GRADED_YES), "severity_attributes": attrs or {}}


# ── impact-aware suppression ──────────────────────────────────────────────────────────────
def test_docs_only_advisory_with_positive_impact_survives() -> None:
    findings = [{"finding": "CONTRIBUTING.md path list contradicts checker", "criteria": ["docs"]}]
    out = _decide(findings, [_verif(0, dict(_IMPACTFUL_ATTRS))])
    assert len(out["surfaced"]) == 1, "docs-only advisory with impact > 0 must surface"
    surfaced = out["surfaced"][0]
    assert surfaced["decision"] == "advisory"
    assert surfaced["impact"] > 0
    assert not out["dropped"]


def test_docs_only_advisory_with_zero_impact_stays_suppressed() -> None:
    findings = [{"finding": "docs nit", "criteria": ["docs"]}]
    out = _decide(findings, [_verif(0)])  # no severity_attributes → impact 0
    assert not out["surfaced"]
    assert len(out["dropped"]) == 1
    dropped = out["dropped"][0]
    assert dropped["decision"] == "dropped"
    assert dropped["reason"] == "nit-suppressed"


def test_llm_prompts_only_zero_impact_stays_suppressed() -> None:
    findings = [{"finding": "prompt nit", "criteria": ["llm-prompts"]}]
    out = _decide(findings, [_verif(0)])
    assert not out["surfaced"]
    assert out["dropped"] and out["dropped"][0]["reason"] == "nit-suppressed"


def test_multi_criteria_advisory_behaviour_unchanged() -> None:
    # Mixed suppressed + non-suppressed criteria surfaces regardless of impact (all-criteria rule).
    findings = [{"finding": "mixed", "criteria": ["docs", "tests"]}]
    out = _decide(findings, [_verif(0)])
    assert len(out["surfaced"]) == 1
    assert out["surfaced"][0]["decision"] == "advisory"
    assert not out["dropped"]


def test_non_suppressed_advisory_behaviour_unchanged() -> None:
    findings = [{"finding": "real", "criteria": ["tests"]}]
    out = _decide(findings, [_verif(0)])
    assert len(out["surfaced"]) == 1
    assert not out["dropped"]


def test_partition_mixes_impactful_and_zero_impact_docs_findings() -> None:
    findings = [
        {"finding": "docs zero-impact nit", "criteria": ["docs"]},
        {"finding": "docs factual contradiction", "criteria": ["docs"]},
    ]
    out = _decide(findings, [_verif(0), _verif(1, dict(_IMPACTFUL_ATTRS))])
    assert len(out["surfaced"]) == 1
    assert out["surfaced"][0]["finding"] == "docs factual contradiction"
    assert len(out["dropped"]) == 1
    assert out["dropped"][0]["finding"] == "docs zero-impact nit"
