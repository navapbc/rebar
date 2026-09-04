"""divergent_implementation divergence-kind grade split (doggish-nonorganic-tsetsefly, plan-v4).

The hard floor for divergent_implementation is keyed on WHICH divergence the finding names:
contradicts_reality / omits_required_site keep the 0.85 auto-high; incomplete_enumeration (an
omitted site that is optional/cosmetic) scores below every blocking threshold and never floors.
This mirrors the ac_unverifiable oracle-kind split (plan-v3, story large-sleepful-needlefish).

Motivating field evidence (plan-v3 corpus, 18,085 verified findings): the axis fired on only
7.72% of findings, and across the 1,307-finding "omitted scope site / unenumerated consumer"
class it exists to describe it was graded `none` 1,173 times (~90%) — so a plan that provably
under-scoped reality scored impact 0.0 and could not block. The regression test below replays the
exact recorded attributes of the finding that motivated this change.

Proving command:
    .venv/bin/pytest tests/unit/test_divergence_grade_split.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from rebar.llm.review_kernel.decide import (
    DIVERGENCE_INCOMPLETE_CONTRIB,
    impact_plan,
    pass3_decide,
    severity_label,
)
from rebar.llm.review_kernel.verify import plan_review_verification_model

pytestmark = pytest.mark.unit

DIVERGENCE_GRADES = (
    "none",
    "incomplete_enumeration",
    "contradicts_reality",
    "omits_required_site",
)
FLOOR_GRADES = ("contradicts_reality", "omits_required_site")
_HARD_FLOOR = 0.85


@pytest.fixture
def rebar_repo(tmp_path, monkeypatch):
    """A self-contained initialized rebar repo (this unit dir has no shared fixture)."""
    import subprocess

    import rebar

    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


# ── closed-set enforcement at verification-parse time ─────────────────────────────────────
def _validate(grade: str, *, strict: bool = False):
    model = plan_review_verification_model(strict=strict)
    return model.model_validate(
        {
            "verifications": [
                {"index": 0, "severity_attributes": {"divergent_implementation": grade}}
            ]
        }
    )


def test_every_divergence_grade_is_accepted() -> None:
    for grade in DIVERGENCE_GRADES:
        v = _validate(grade)
        assert v.verifications[0].severity_attributes.divergent_implementation == grade


def test_out_of_vocabulary_grade_is_rejected() -> None:
    # The failure path of the closed-set contract: the legacy ordinal ladder and arbitrary
    # strings are parse errors, in strict AND non-strict modes (Literal is unconditional).
    for bad in ("low", "medium", "high", "divergent", ""):
        for strict in (False, True):
            with pytest.raises(pydantic.ValidationError):
                _validate(bad, strict=strict)


# ── the impact model: which grades floor, which are coached ────────────────────────────────
def _attrs(grade: str, **extra) -> dict:
    return {"divergent_implementation": grade, **extra}


def test_floor_grades_are_auto_high() -> None:
    # Both floor grades map to a full 1.0 contribution (the broken_oracle/missing_oracle
    # precedent), so they land above the 0.85 floor rather than merely at it.
    for grade in FLOOR_GRADES:
        assert impact_plan(_attrs(grade)) == 1.0
        assert impact_plan(_attrs(grade)) >= _HARD_FLOOR
        assert severity_label(impact_plan(_attrs(grade))) == "critical"


def test_floor_survives_the_self_revealing_amplifier() -> None:
    # The override is floored LAST, after the detection amplifier — a self-revealing
    # divergence must still land >= 0.85, not 0.85 x 0.8 = 0.68.
    for grade in FLOOR_GRADES:
        attrs = _attrs(grade, silent_vs_self_revealing="self_revealing")
        assert impact_plan(attrs) == _HARD_FLOOR


def test_incomplete_enumeration_contributes_but_never_floors() -> None:
    imp = impact_plan(_attrs("incomplete_enumeration"))
    assert imp == DIVERGENCE_INCOMPLETE_CONTRIB
    assert imp < _HARD_FLOOR


def test_none_contributes_nothing() -> None:
    assert impact_plan(_attrs("none")) == 0.0
    assert impact_plan({}) == 0.0  # absent axis abstains, never inflates


def test_incomplete_contrib_stays_below_every_blocking_threshold() -> None:
    """INVARIANT (mirrors the UNDERSPECIFIED_ORACLE_CONTRIB pin): the coached grade must score
    below the LOWEST blocking block_threshold in the packaged routing, so a future
    recalibration that drops a threshold under it fails loudly here instead of silently
    turning every cosmetic omission into a block."""
    routing = json.loads(
        (
            Path(__file__).resolve().parents[2] / "src/rebar/llm/plan_review/criteria_routing.json"
        ).read_text()
    )
    blocking = [
        v["block_threshold"]
        for v in routing.values()
        if isinstance(v, dict) and v.get("default_posture") == "blocking"
    ]
    assert blocking, "expected at least one blocking criterion in the packaged routing"
    assert DIVERGENCE_INCOMPLETE_CONTRIB < min(blocking)


# ── the regression this change exists for ──────────────────────────────────────────────────
# Recorded attributes of finding fc347224733dccc6b on epic c4ad-a93e-0613-408f
# (sec-semantic-layer-2026-challenge): a G6 finding naming four sites that branch on the literal
# "s3vectors" and were omitted from the plan's scope. It scored impact 0.0 / severity none and
# landed ADVISORY at validity 0.8889 against G6's block_threshold 0.60.
_RECORDED_BINARY = {
    "absence_confirmed_in_context": "yes",
    "asserted_capability_confirmed": "yes",
    "cited_reference_accurate": "yes",
    "claims_absence": "yes",
    "committed_work_relies_on_unbacked_claim": "na",
    "evidence_entails_finding": "yes",
    "impact_follows_necessarily": "yes",
    "is_verifiable": "yes",
    "no_existing_mitigation": "no",
    "no_viable_alternative_explanation": "yes",
    "path_reachable": "yes",
    "prerequisite_attribution_valid": "na",
    "respects_artifact_altitude": "yes",
    "severity_claim_justified": "yes",
}
_RECORDED_ATTRS = {
    "ac_unverifiable": "none",
    "blast_radius": "module",
    "debt_impact": "low",
    "dod_uncertifiable": "none",
    "internal_conflict": "none",
    "irreversible_without_rationale": "none",
    "likelihood": "medium",
    "prod_impact": "medium",
    "reversibility": "easy",
    "silent_vs_self_revealing": "self_revealing",
    "undecomposed": "none",
    "vague_directive": "none",
}
_G6_THRESHOLD = 0.60


def _decide(divergence_grade: str) -> dict:
    attrs = {**_RECORDED_ATTRS, "divergent_implementation": divergence_grade}
    return pass3_decide(
        {"binary": _RECORDED_BINARY, "severity_attributes": attrs},
        block_threshold=_G6_THRESHOLD,
        blocking_enabled=True,
        impact_fn=impact_plan,
    )


def test_recorded_finding_still_scores_zero_when_ungraded() -> None:
    # The pre-fix behaviour, pinned so the regression is unambiguous: with the axis `none`
    # every one of the seven axes is none, the MAX is 0.0, and a high-validity finding
    # describing a mechanism that would not work lands advisory.
    d = _decide("none")
    assert d["impact"] == 0.0
    assert "severity" not in d  # the impact-only label is retired from pass3_decide's output
    assert d["decision"] == "advisory"
    assert d["validity"] == pytest.approx(0.8889, abs=1e-4)


def test_recorded_finding_blocks_when_graded_omits_required_site() -> None:
    # The fix: the same finding, graded for the divergence it actually describes, clears G6's
    # 0.60 bar. 0.8889 x 0.85 = 0.7556.
    d = _decide("omits_required_site")
    assert d["impact"] == _HARD_FLOOR
    assert d["priority"] == pytest.approx(0.7556, abs=1e-4)
    assert d["decision"] == "block"
    assert "severity" not in d  # the impact-only label is retired from pass3_decide's output


def test_recorded_finding_stays_advisory_when_merely_cosmetic() -> None:
    # The pressure-release valve: had the omission been optional/cosmetic, the finding is
    # coached, not blocked. The recorded finding is self_revealing, so the detection amplifier
    # applies to the coached contribution (0.55 x 0.8 = 0.44) — it does NOT floor, which is the
    # whole point of the grade; priority 0.8889 x 0.44 = 0.3911 < 0.60.
    d = _decide("incomplete_enumeration")
    assert d["impact"] == pytest.approx(DIVERGENCE_INCOMPLETE_CONTRIB * 0.8, abs=1e-4)
    assert d["decision"] == "advisory"
    assert d["priority"] < _G6_THRESHOLD


# ── operator-attested enrich asymmetry ─────────────────────────────────────────────────────
_OA_DESC = (
    "A plan with an operational criterion.\n\n## Acceptance Criteria\n"
    "- [ ] [operator-attested] the fix is deployed to prod and the gate passes\n"
)


def _enriched_axis(grade: str) -> str:
    from rebar.llm.plan_review import decide_ops

    finding = {
        "checklist_item": "[operator-attested] the fix is deployed to prod and the gate passes",
        "evidence": [],
    }
    verification = {"severity_attributes": {"divergent_implementation": grade}}
    decide_ops.enrich_operator_attested([finding], {0: verification}, _OA_DESC)
    return verification["severity_attributes"]["divergent_implementation"]


def test_attestation_clears_cosmetic_but_never_a_floor_grade() -> None:
    # Attesting an outcome does not make a false claim about the code true, nor conjure a
    # required site the plan omits — so both floor grades survive enrichment.
    assert _enriched_axis("incomplete_enumeration") == "none"
    for grade in FLOOR_GRADES:
        assert _enriched_axis(grade) == grade


# ── sidecar persistence + legacy read-as-is ────────────────────────────────────────────────
def test_grade_persists_in_sidecar_payload_and_stamps_the_cohort() -> None:
    from rebar.llm.plan_review import sidecar
    from rebar.llm.plan_review.sidecar import build_payload

    verdict = {
        "verdict": "BLOCK",
        "ticket_id": "t",
        "blocking": [
            {
                "id": "f1",
                "criteria": ["G6"],
                "finding": "x",
                "verification": {
                    "severity_attributes": {"divergent_implementation": "omits_required_site"}
                },
            }
        ],
    }
    payload = build_payload(verdict, material="m")
    sa = payload["findings"][0]["verification"]["severity_attributes"]
    assert sa["divergent_implementation"] == "omits_required_site"
    # The payload carries WHATEVER cohort tag is current (ADR 0036 no-pooling; ADR 0054 batching).
    # Asserted against the constant so a later bump does not need to edit this grade-persistence
    # test; the authoritative literal pin lives in test_impact_model_versioning.py.
    assert payload["impact_model_version"] == sidecar.IMPACT_MODEL_VERSION


def test_legacy_ordinal_sidecar_reads_as_is(rebar_repo: Path) -> None:
    # Back-compat is segmentation, not migration: a stored plan-v3 record carrying the legacy
    # ordinal grade round-trips unmodified through the sidecar readers.
    from rebar.llm.plan_review import sidecar

    tracker = Path(rebar_repo) / ".tickets-tracker" / "t-legacy"
    tracker.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema": "plan_review_result_v2",
        "ticket_id": "t-legacy",
        "verdict": "BLOCK",
        "impact_model_version": "plan-v3",
        "findings": [
            {
                "id": "f1",
                "verification": {"severity_attributes": {"divergent_implementation": "high"}},
            }
        ],
    }
    (tracker / "1700000000000000000-aaaa-REVIEW_RESULT.json").write_text(
        json.dumps({"event_type": "REVIEW_RESULT", "data": legacy})
    )
    got = sidecar.latest_review_result("t-legacy", repo_root=str(rebar_repo))
    assert got is not None
    assert got["impact_model_version"] == "plan-v3"
    sa = got["findings"][0]["verification"]["severity_attributes"]
    assert sa["divergent_implementation"] == "high"
