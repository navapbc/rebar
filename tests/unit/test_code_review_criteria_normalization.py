"""Criteria-synonym normalization before routing (ticket d890-e711-156e-444b).

On the code-v3 corpus the model emitted ``sec`` (8) and ``documentation`` (5) — synonyms of
the packaged criteria ``security`` and ``docs``. Because they matched no known criterion id
they fell to the default 0.95 posture instead of their criterion's tuned routing.
``registry.normalize_criteria`` is a synonym MAPPING, not a whitelist: a label already
present in the EFFECTIVE vocabulary (project overlay included, never the packaged index
alone) is never rewritten; a ``CRITERIA_SYNONYMS`` key maps to its canonical id; any other
label passes through UNCHANGED, so prompt-specified open dimensions (maintainability /
correctness / edge-cases) and project criteria are unaffected. ``code_review_decide``
applies it BEFORE the pass3 threshold lookup and before nit-suppression, so a normalized
label gets its criterion's tuned routing.

Proving command:
    .venv/bin/pytest tests/unit/test_code_review_criteria_normalization.py -v
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

# contract_drift (maintainability, moderate tier 0.6) with churn90 absent (freq_mult 0.5) and
# no silence amplifier (amp 0.8) → impact_code = 0.6 * 0.5 * 0.8 = 0.24 > 0.
_IMPACTFUL_ATTRS = {"contract_drift": "yes"}

# data_loss_without_recovery (serious tier 0.9) + silent_failure (amp 1.0) → impact 0.9, so
# priority = validity 1.0 × 0.9 = 0.9: above security's tuned block line (0.54, blocking
# enabled) yet below the 0.95 default an unknown label would resolve to.
_BLOCKING_ATTRS = {"data_loss_without_recovery": "yes", "silent_failure": "yes"}


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


def _overlay_repo(tmp_path):
    """A repo whose overlay activates the project criterion ``project.foo`` for code review."""
    repo = tmp_path / "repo"
    (repo / ".rebar").mkdir(parents=True)
    (repo / ".rebar" / "criteria_routing.json").write_text(
        json.dumps(
            {
                "code_review": {
                    "project.foo": {
                        "exec": "1-TURN",
                        "applies_to": [],
                        "default_posture": "advisory",
                        "block_threshold": 0.8,
                        "blocking_enabled": False,
                    }
                },
                "activate": ["project.foo"],
            }
        ),
        encoding="utf-8",
    )
    return repo


# ── the mapping itself ────────────────────────────────────────────────────────────────────
def test_normalize_maps_the_two_synonyms_to_their_canonical_ids() -> None:
    assert registry.normalize_criteria(["sec", "documentation"]) == ["security", "docs"]


def test_normalize_never_rewrites_a_known_id() -> None:
    assert registry.normalize_criteria(["security", "docs"]) == ["security", "docs"]


def test_normalize_passes_open_dimensions_and_unknown_labels_through() -> None:
    labels = ["maintainability", "correctness", "edge-cases", "made-up-label"]
    assert registry.normalize_criteria(labels) == labels


def test_normalize_dedupes_when_a_synonym_and_its_canonical_id_co_occur() -> None:
    assert registry.normalize_criteria(["sec", "security"]) == ["security"]


def test_effective_vocabulary_membership_beats_the_synonym_map(tmp_path, monkeypatch) -> None:
    """Known-id membership is resolved through the EFFECTIVE vocabulary (project overlay
    included), not the packaged index: a synthetic synonym entry for an overlay-activated
    project id rewrites WITHOUT the overlay and is left alone WITH it."""
    repo = _overlay_repo(tmp_path)
    assert "project.foo" in registry.effective_criteria(str(repo))
    monkeypatch.setitem(registry.CRITERIA_SYNONYMS, "project.foo", "security")
    # Packaged-only vocabulary (no overlay): the synthetic synonym key rewrites.
    assert registry.normalize_criteria(["project.foo"], str(tmp_path)) == ["security"]
    # Overlay-activated: the label is in the effective vocabulary — never rewritten.
    assert registry.normalize_criteria(["project.foo"], str(repo)) == ["project.foo"]


# ── decide-level: normalization happens BEFORE routing/threshold lookup ─────────────────────
def test_sec_normalizes_to_security_and_gets_its_tuned_blocking_routing() -> None:
    findings = [{"finding": "credentials logged in cleartext", "criteria": ["sec"]}]
    out = _decide(findings, [_verif(0, dict(_BLOCKING_ATTRS))])
    assert len(out["blocking"]) == 1, (
        "a sec-labelled finding must resolve security's tuned routing (block at 0.54), "
        "not the 0.95 unknown-label default"
    )
    blocked = out["blocking"][0]
    assert blocked["criteria"] == ["security"]
    assert blocked["decision"] == "block"
    assert not out["surfaced"]


def test_documentation_normalizes_to_docs_and_inherits_nit_suppression() -> None:
    findings = [{"finding": "docs nit", "criteria": ["documentation"]}]
    out = _decide(findings, [_verif(0)])  # no severity_attributes → impact 0
    assert not out["surfaced"], (
        "a documentation-labelled zero-impact advisory must inherit docs' nit_suppressed "
        "routing, not surface under the unknown-label default"
    )
    dropped = out["dropped"][0]
    assert dropped["reason"] == "nit-suppressed"
    assert dropped["criteria"] == ["docs"]


def test_docs_finding_with_positive_impact_still_surfaces_after_normalization() -> None:
    # Pins the Item-4 property: nit-suppression is impact-aware, so normalization into the
    # nit-suppressed `docs` criterion never hides a high-impact documentation finding.
    findings = [{"finding": "docs factual contradiction", "criteria": ["documentation"]}]
    out = _decide(findings, [_verif(0, dict(_IMPACTFUL_ATTRS))])
    assert len(out["surfaced"]) == 1
    surfaced = out["surfaced"][0]
    assert surfaced["criteria"] == ["docs"]
    assert surfaced["impact"] > 0
    assert not out["dropped"]


def test_open_dimension_and_project_labels_pass_through_decide_unchanged() -> None:
    findings = [
        {"finding": "maint observation", "criteria": ["maintainability"]},
        {"finding": "phase boundary", "criteria": ["project.review-phase-boundaries"]},
    ]
    out = _decide(findings, [_verif(0), _verif(1)])
    surfaced_criteria = [f["criteria"] for f in out["surfaced"]]
    assert ["maintainability"] in surfaced_criteria
    assert ["project.review-phase-boundaries"] in surfaced_criteria
    assert not out["dropped"]
