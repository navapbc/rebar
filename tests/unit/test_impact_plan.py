"""Plan-review impact redesign (story fishable-apivorous-redhead).

Covers ``decide.impact_plan`` (severity-first MAX + hard-override floor + detection amplifier)
and the per-gate ``impact_fn`` dispatch threaded through ``pass3_decide`` / ``pass3_over_findings``.

Proving command:
    .venv/bin/pytest tests/unit/test_impact_plan.py -v
"""

from __future__ import annotations

from rebar.llm.review_kernel import decide
from rebar.llm.review_kernel.decide import (
    DOD_UNDERSPECIFIED_CONTRIB,
    UNDECOMPOSED_BUNDLED_CONTRIB,
    impact,
    impact_plan,
    pass3_decide,
    pass3_over_findings,
)


# ── impact_plan: MAX aggregation ─────────────────────────────────────────────────────────
def test_empty_attrs_is_zero() -> None:
    # An older/absent verifier that emits no axes ABSTAINS: impact_plan = 0 (never inflates).
    assert impact_plan({}) == 0.0


def test_absent_new_axes_do_not_inflate() -> None:
    # Only the OLD base attributes present (no plan axes) -> still 0 (back-compat abstain).
    assert impact_plan({"prod_impact": "high", "blast_radius": "system"}) == 0.0


def test_max_not_mean() -> None:
    # Two medium axes must not average down; MAX takes the higher single axis.
    attrs = {"internal_conflict": "medium", "vague_directive": "medium"}
    assert impact_plan(attrs) == round(decide._SEV01["medium"], 4)  # 0.67, not (0.67+0.67)/2


def test_single_high_non_override_axis() -> None:
    assert impact_plan({"internal_conflict": "high"}) == 1.0


# ── impact_plan: hard override ───────────────────────────────────────────────────────────
def test_hard_override_floors_gap_kind_to_at_least_085() -> None:
    # plan-v5: dod_uncertifiable is KIND-graded, so the floor is decided by kind, not by any
    # non-none grade. A genuine-gap kind is a hard override -> at or above the 0.85 floor.
    assert impact_plan({"dod_uncertifiable": "uncertifiable_outcome"}) >= 0.85


def test_every_override_axis_triggers_floor_on_its_gap_kinds() -> None:
    # plan-v5: ALL FOUR override axes are now closed kind sets. Each one's genuine-gap kinds
    # floor; each one's specificity-only kind does not (pinned per axis below and in the
    # per-axis suites test_oracle_grade_split.py / test_divergence_grade_split.py).
    gap_kinds = {
        "ac_unverifiable": ("missing_oracle", "broken_oracle"),
        "divergent_implementation": ("contradicts_reality", "omits_required_site"),
        "undecomposed": ("missing_required_child", "no_executable_breakdown"),
        "dod_uncertifiable": ("uncertifiable_outcome", "certification_cannot_prove"),
    }
    for axis, kinds in gap_kinds.items():
        for kind in kinds:
            assert impact_plan({axis: kind}) >= 0.85, f"{axis}={kind}"


def test_retired_ordinal_grades_no_longer_score_on_the_kind_graded_axes() -> None:
    # The ordinal vocabulary is RETIRED on the kind-graded axes: `_SEV01` has no entry for a kind
    # name and the kind maps have no entry for an ordinal, so a stale 'low' contributes nothing
    # and never floors. This is the plan-v5 cohort boundary made executable — a pre-v5 sidecar
    # must be re-scored through its own recorded grades, not replayed through this model.
    for axis in ("dod_uncertifiable", "undecomposed"):
        for grade in ("low", "medium", "high"):
            assert impact_plan({axis: grade}) == 0.0, f"{axis}={grade}"


# ── impact_plan: ac_unverifiable oracle-kind grades (story large-sleepful-needlefish) ────
def test_missing_and_broken_oracle_keep_the_floor() -> None:
    assert impact_plan({"ac_unverifiable": "missing_oracle"}) >= 0.85
    assert impact_plan({"ac_unverifiable": "broken_oracle"}) >= 0.85


def test_underspecified_oracle_never_floors() -> None:
    # The dominant floor-driven class (56% of the calibration-3 sample) is a refinement
    # demand: it surfaces and is coached but scores below every blocking threshold.
    v = impact_plan({"ac_unverifiable": "underspecified_oracle"})
    assert v == decide.UNDERSPECIFIED_ORACLE_CONTRIB
    assert v < 0.60


def test_underspecified_contrib_stays_below_lowest_blocking_threshold() -> None:
    # INVARIANT: a future threshold recalibration below the contrib must fail loudly here,
    # not silently re-enable auto-blocks for specificity demands.
    import json
    from importlib import resources

    routing = json.loads(
        (resources.files("rebar.llm.plan_review") / "criteria_routing.json").read_text()
    )
    blocking_thrs = [
        e["block_threshold"]
        for e in routing.values()
        if isinstance(e, dict) and e.get("default_posture") == "blocking"
    ]
    assert decide.UNDERSPECIFIED_ORACLE_CONTRIB < min(blocking_thrs)


def test_legacy_ordinal_grade_maps_to_zero_under_plan_v3() -> None:
    # plan-v3 never scores legacy records (ADR 0036 segmentation); if a legacy string reaches
    # the scorer anyway it contributes nothing rather than silently flooring.
    assert impact_plan({"ac_unverifiable": "low"}) == 0.0


def test_non_override_axis_does_not_floor() -> None:
    # internal_conflict is NOT an override axis: a 'low' stays low.
    assert impact_plan({"internal_conflict": "low"}) == round(decide._SEV01["low"], 4)


# ── impact_plan: detection amplifier + the compose-order coherence fix ───────────────────
def test_self_revealing_dampens_non_override() -> None:
    # internal_conflict high (1.0) * self-revealing (0.8) = 0.8.
    assert (
        impact_plan({"internal_conflict": "high", "silent_vs_self_revealing": "self_revealing"})
        == 0.8
    )


def test_silent_is_full_weight() -> None:
    assert impact_plan({"internal_conflict": "high", "silent_vs_self_revealing": "silent"}) == 1.0


def test_override_survives_self_revealing_amplifier() -> None:
    # The coherence fix (COH/E1/G6): the ticket's literal compose would give
    # 0.85 * 0.8 = 0.68 (< 0.70) for a self-revealing override finding, defeating "auto-high".
    # Flooring the override LAST guarantees it stays >= 0.85. Uses undecomposed's gap kind
    # (plan-v5); the same property is pinned per axis in the oracle/divergence suites.
    attrs = {"undecomposed": "missing_required_child", "silent_vs_self_revealing": "self_revealing"}
    assert impact_plan(attrs) >= 0.85


def test_dod_uncertifiable_forces_full_detection_weight() -> None:
    # dod_uncertifiable forces mult=1.0 for ANY non-none kind — including the advisory one, which
    # does NOT floor. This is the regression the plan-v5 conversion could silently cause: the
    # force used to read `_SEV01`, which returns 0.0 for every kind name, so reading it through
    # the old map would drop the amplifier force to 0.8 and score this 0.44 instead of 0.55.
    advisory = {
        "dod_uncertifiable": "underspecified_certification",
        "silent_vs_self_revealing": "self_revealing",
    }
    assert impact_plan(advisory) == DOD_UNDERSPECIFIED_CONTRIB
    # ...and a genuine-gap kind is still auto-high regardless of a self-revealing tag.
    assert (
        impact_plan(
            {
                "dod_uncertifiable": "certification_cannot_prove",
                "silent_vs_self_revealing": "self_revealing",
            }
        )
        >= 0.85
    )


def test_deterministic() -> None:
    # AC4: verdicts stay stable on same-material pairs -> impact_plan is a pure function.
    attrs = {
        "ac_unverifiable": "missing_oracle",
        "vague_directive": "high",
        "silent_vs_self_revealing": "silent",
    }
    assert impact_plan(attrs) == impact_plan(dict(attrs))


# ── AC4: previously-stranded 0.60-0.69 band findings clear 0.70 ──────────────────────────
def test_stranded_band_finding_now_clears_070() -> None:
    # A genuinely critical finding whose base attributes average into the 0.60-0.69 dead band
    # under the OLD mean model. The same finding, scored by a hard-override plan axis, clears 0.70.
    attrs = {
        "prod_impact": "high",
        "blast_radius": "module",
        "likelihood": "medium",
        "reversibility": "easy",
        "ac_unverifiable": "missing_oracle",  # the plan-severity signal the mean ignored
    }
    assert impact_plan(attrs) >= 0.70  # cleared under the redesign


# ── per-gate impact_fn dispatch ──────────────────────────────────────────────────────────
def _verif(attrs: dict, *, binary: dict | None = None) -> dict:
    # A verification whose binary answers are all "yes" -> validity 1.0, so the impact scalar
    # equals priority and drives the decision.
    from rebar.llm.review_kernel import GRADED_BINARY

    b = {q: "yes" for q in GRADED_BINARY}
    b["cited_reference_accurate"] = "yes"
    if binary:
        b.update(binary)
    return {"severity_attributes": attrs, "binary": b}


def test_pass3_decide_defaults_to_mean_impact() -> None:
    # AC1: a caller that omits impact_fn (the code-review path today) is byte-unchanged -> the
    # decision's `impact` equals the mean `impact(attrs)`.
    attrs = {"prod_impact": "high", "ac_unverifiable": "high"}
    d = pass3_decide(_verif(attrs), block_threshold=0.7, blocking_enabled=True)
    assert d["impact"] == impact(attrs)


def test_pass3_decide_uses_impact_fn_when_given() -> None:
    attrs = {"prod_impact": "high", "ac_unverifiable": "missing_oracle"}
    d = pass3_decide(
        _verif(attrs), block_threshold=0.7, blocking_enabled=True, impact_fn=impact_plan
    )
    assert d["impact"] == impact_plan(attrs)
    assert d["impact"] != impact(attrs)  # the two models genuinely differ on this finding


def test_pass3_over_findings_threads_impact_fn() -> None:
    findings = [{"finding": "x", "criteria": ["E2"]}]
    verifs = {0: _verif({"ac_unverifiable": "missing_oracle"})}
    plan = pass3_over_findings(
        findings, verifs, threshold_for=lambda c: (0.7, True), impact_fn=impact_plan
    )
    base = pass3_over_findings(findings, verifs, threshold_for=lambda c: (0.7, True))
    assert plan[0]["impact"] == impact_plan({"ac_unverifiable": "missing_oracle"})
    assert base[0]["impact"] == impact({"ac_unverifiable": "missing_oracle"})


# ── plan-v5 kind sets: undecomposed + dod_uncertifiable (story fixable-angular-caribou) ──
def test_undecomposed_bundling_is_advisory_and_never_floors() -> None:
    # The class the conversion exists for: 23 of the 30 recorded `undecomposed` gradings say
    # "this plan bundles N independently-releasable outcomes" — a right-sizing note on a plan
    # that is executable as written — yet every one of them floored to 0.85 under the ordinal
    # ladder. It must now contribute below every blocking threshold instead.
    assert impact_plan({"undecomposed": "bundles_separable_slices"}) == UNDECOMPOSED_BUNDLED_CONTRIB
    assert impact_plan({"undecomposed": "bundles_separable_slices"}) < 0.85


def test_dod_specificity_kind_is_advisory_and_never_floors() -> None:
    assert (
        impact_plan({"dod_uncertifiable": "underspecified_certification"})
        == DOD_UNDERSPECIFIED_CONTRIB
    )
    assert impact_plan({"dod_uncertifiable": "underspecified_certification"}) < 0.85


def test_plan_v5_advisory_contribs_stay_below_every_blocking_threshold() -> None:
    # INVARIANT (mirrors the UNDERSPECIFIED_ORACLE_CONTRIB pin): a future recalibration that
    # lowers any blocking block_threshold to or below an advisory contribution would silently
    # turn these coached grades into auto-blocks. Fail loudly instead.
    import json
    from importlib import resources

    routing = json.loads(
        (resources.files("rebar.llm.plan_review") / "criteria_routing.json").read_text()
    )
    blocking_thrs = [
        e["block_threshold"]
        for e in routing.values()
        if isinstance(e, dict) and e.get("default_posture") == "blocking"
    ]
    assert blocking_thrs, "no blocking thresholds found — the invariant would be vacuous"
    for contrib in (UNDECOMPOSED_BUNDLED_CONTRIB, DOD_UNDERSPECIFIED_CONTRIB):
        assert contrib < min(blocking_thrs), (contrib, min(blocking_thrs))


def test_plan_v5_axes_are_closed_literal_kind_sets() -> None:
    # The vocabulary is enforced at verification-parse time, not just by convention: the model
    # must reject the retired ordinal labels outright.
    import typing

    from rebar.llm.review_kernel.verify_models import plan_review_verification_model

    outer = plan_review_verification_model()
    verification = typing.get_args(outer.model_fields["verifications"].annotation)[0]
    attrs_model = verification.model_fields["severity_attributes"].annotation
    for axis, expected in (
        (
            "undecomposed",
            {
                "none",
                "bundles_separable_slices",
                "missing_required_child",
                "no_executable_breakdown",
            },
        ),
        (
            "dod_uncertifiable",
            {
                "none",
                "underspecified_certification",
                "uncertifiable_outcome",
                "certification_cannot_prove",
            },
        ),
    ):
        allowed = set(typing.get_args(attrs_model.model_fields[axis].annotation))
        assert allowed == expected, axis
        assert not allowed & {"low", "medium", "high"}, axis
