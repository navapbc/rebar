"""Code-review near-term nit reduction (story grusome-uncheerful-nematode).

The `docs` + `llm-prompts` advisory surfaces are the lowest-value nit sources (0/9 high-value in
the dogfooded adjudication). A `nit_suppressed` routing flag demotes an advisory finding whose
criteria are ALL nit-suppressed from the surfaced set to `dropped`, WITHOUT touching the blocking
posture. Covers the packaged routing, the `nit_suppressed_criteria` registry helper, and the
`code_review_decide` partition demotion.

Proving command:
    .venv/bin/pytest tests/unit/test_code_review_nit_routing.py -v
"""

from __future__ import annotations

import json

import pytest

import rebar.llm.workflow.executor as _ex
from rebar.llm.code_review import registry
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


# ── packaged routing ──────────────────────────────────────────────────────────────────────
def test_packaged_routing_marks_docs_and_llm_prompts_nit_suppressed() -> None:
    idx = registry.routing_index()
    for key in ("docs", "llm-prompts"):
        assert idx[key]["nit_suppressed"] is True, f"{key} must be nit_suppressed"
        assert idx[key]["blocking_enabled"] is False, f"{key} must stay non-blocking"


def test_other_advisory_overlays_are_not_nit_suppressed() -> None:
    idx = registry.routing_index()
    for key in ("security", "tests", "performance", "api-compat", "db-migrations"):
        assert not idx[key].get("nit_suppressed"), f"{key} must NOT be nit_suppressed"


def test_routing_file_is_valid_json() -> None:
    from importlib import resources

    raw = resources.files("rebar.llm.code_review").joinpath("criteria_routing.json").read_text()
    json.loads(raw)  # must parse


# ── nit_suppressed_criteria helper ────────────────────────────────────────────────────────
def test_nit_suppressed_criteria_returns_docs_and_llm_prompts() -> None:
    assert registry.nit_suppressed_criteria() == frozenset({"docs", "llm-prompts"})


def test_nit_suppressed_criteria_honours_a_passed_map() -> None:
    custom = {"x": {"nit_suppressed": True}, "y": {"blocking_enabled": False}, "z": {}}
    assert registry.nit_suppressed_criteria(custom) == frozenset({"x"})


# ── code_review_decide partition demotion ─────────────────────────────────────────────────
def test_docs_only_advisory_is_demoted_to_dropped() -> None:
    findings = [{"finding": "nit", "criteria": ["docs"]}]
    out = _decide(findings, [_verif(0)])
    assert not out["surfaced"]
    assert len(out["dropped"]) == 1
    dropped = out["dropped"][0]
    assert dropped["decision"] == "dropped"
    assert dropped["reason"] == "nit-suppressed"


def test_mixed_criteria_advisory_still_surfaces() -> None:
    # A finding mapping to a nit-suppressed AND a non-suppressed criterion still surfaces.
    findings = [{"finding": "mixed", "criteria": ["docs", "tests"]}]
    out = _decide(findings, [_verif(0)])
    assert len(out["surfaced"]) == 1
    assert out["surfaced"][0]["decision"] == "advisory"
    assert not out["dropped"]


def test_non_suppressed_advisory_still_surfaces() -> None:
    findings = [{"finding": "real", "criteria": ["tests"]}]
    out = _decide(findings, [_verif(0)])
    assert len(out["surfaced"]) == 1
    assert not out["dropped"]


def test_llm_prompts_only_advisory_is_demoted() -> None:
    findings = [{"finding": "prompt nit", "criteria": ["llm-prompts"]}]
    out = _decide(findings, [_verif(0)])
    assert not out["surfaced"]
    assert out["dropped"] and out["dropped"][0]["reason"] == "nit-suppressed"


def test_partition_mixes_suppressed_and_surfaced() -> None:
    findings = [
        {"finding": "docs nit", "criteria": ["docs"]},
        {"finding": "tests real", "criteria": ["tests"]},
        {"finding": "prompt nit", "criteria": ["llm-prompts"]},
    ]
    out = _decide(findings, [_verif(0), _verif(1), _verif(2)])
    assert len(out["surfaced"]) == 1
    assert out["surfaced"][0]["finding"] == "tests real"
    assert len(out["dropped"]) == 2
    assert all(f["reason"] == "nit-suppressed" for f in out["dropped"])


def test_finding_with_no_criteria_is_not_demoted() -> None:
    # An advisory with an empty criteria list is NOT all-nit-suppressed (vacuous guard) → surfaces.
    findings = [{"finding": "uncategorised", "criteria": []}]
    out = _decide(findings, [_verif(0)])
    assert len(out["surfaced"]) == 1
    assert not out["dropped"]
