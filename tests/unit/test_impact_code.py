"""Code-review impact model — code-v4 (bug obese-dihedral-ermine).

Covers ``decide.impact_code``, the code-v4 maintainability-lane split (serious undamped /
moderate prod_impact-keyed / debt churn-amplifier-only), the two lane-scoped trigger maps
derived from the single ``trigger_likelihood`` field, the consequence-lane-only reversibility
floor, the new serious-maint binary ``forbids_contract_allowed_state``, the per-gate
``impact_fn`` dispatch, the DET-enrichment helpers, and the labeled-fixture calibration
(HIGH vs NIT separation) re-verified under v4.

v4 model (per lane, all ∈ [0,1]):
  prod_lane      = MAX(tier of TRUE prod binaries) × prod_trigger_mult
                   (common 1.0 / sometimes 0.6 / rare 0.25)
  serious_maint  = MAX(tier of TRUE serious-maint binaries) × 1.0   (UNDAMPED — no churn)
  moderate_maint = MAX(tier of TRUE moderate-maint binaries)
                   × prod_impact_mult(high/med 1.0, low 0.6, none 0.5)
                   × moderate_trigger_mult(rare 0.75, sometimes/common 1.0)
  debt_lane      = MAX(tier of TRUE debt binaries) × churn_amp
                   (churn_amp = 1.0 + 0.5·min(churn90,30)/30 ∈ [1.0,1.5] — AMPLIFIER ONLY)
  impact_base      = MAX over the four lanes
  consequence_base = MAX(prod, serious_maint, moderate_maint)   (EXCLUDES debt)
  amp   = 1.0 if silent_failure|escapes_automation else 0.8
  floor = 0.6 iff consequence_base > 0 AND hard_to_reverse_surface (debt NEVER floors)
  impact = round(max(min(1.0, impact_base × amp), floor), 4)

Proving command:
    .venv/bin/pytest tests/unit/test_impact_code.py -v
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from rebar.llm.review_kernel.decide import (
    impact,
    impact_code,
    impact_plan,
    pass3_decide,
    pass3_over_findings,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "code_review_impact_labels.jsonl"


# ── impact_code: abstain / no-inflation ───────────────────────────────────────────────────
def test_empty_attrs_is_zero() -> None:
    # An older/absent verifier that emits no consequence binaries ABSTAINS: impact 0.
    assert impact_code({}) == 0.0


def test_absent_binaries_do_not_inflate() -> None:
    # Only base severity attributes present (no consequence binaries) -> still 0. prod_impact
    # is read ONLY by the moderate-maint lane, so a high prod_impact with no moderate binary
    # contributes nothing.
    assert (
        impact_code({"prod_impact": "high", "blast_radius": "system", "reversibility": "hard"})
        == 0.0
    )


# ── impact_code: PRODUCTION lane (unchanged from v3) ──────────────────────────────────────
def test_serious_prod_binary_common_notsilent() -> None:
    # serious(0.9) * common(1.0) prod_lane; amp 0.8 (not silent) -> 0.72.
    assert impact_code({"data_loss_without_recovery": True}) == 0.72


def test_moderate_prod_binary() -> None:
    # capability_degraded moderate(0.6) * common(1.0) * amp 0.8 -> 0.48.
    assert impact_code({"capability_degraded": True}) == 0.48


def test_lane_max_not_sum_no_compounding() -> None:
    # Two serious prod binaries do not compound: MAX(0.9, 0.9) = 0.9, not 1.8.
    both = impact_code(
        {"data_loss_without_recovery": True, "silent_wrong_feeding_a_decision": True}
    )
    one = impact_code({"data_loss_without_recovery": True})
    assert both == one == 0.72


def test_trigger_likelihood_scales_prod_lane() -> None:
    base = {"data_loss_without_recovery": True}
    assert impact_code({**base, "trigger_likelihood": "common"}) == 0.72
    assert impact_code({**base, "trigger_likelihood": "sometimes"}) == 0.432
    assert impact_code({**base, "trigger_likelihood": "rare"}) == 0.18


def test_trigger_likelihood_absent_is_common() -> None:
    # Absent trigger_likelihood must NOT dampen a serious correctness finding (defaults common).
    assert impact_code({"data_loss_without_recovery": True}) == impact_code(
        {"data_loss_without_recovery": True, "trigger_likelihood": "common"}
    )


# ── impact_code: SERIOUS maintainability lane — UNDAMPED (v4) ──────────────────────────────
def test_serious_maint_undamped_by_churn() -> None:
    # v4: serious-maint binaries score at full tier × 1.0 — NO churn/freq dampener. A cold
    # serious contract break scores the same as a hot one (both 0.72 not-silent; 0.9 silent).
    cold = impact_code({"safety_net_removal_without_replacement": True})
    hot = impact_code({"safety_net_removal_without_replacement": True, "churn90": 30})
    assert cold == hot == 0.72
    assert (
        impact_code({"safety_net_removal_without_replacement": True, "silent_failure": True}) == 0.9
    )


def test_unversioned_contract_break_serious_maint() -> None:
    assert impact_code({"unversioned_published_contract_break": True}) == 0.72
    assert (
        impact_code({"unversioned_published_contract_break": True, "silent_failure": True}) == 0.9
    )


def test_forbids_contract_allowed_state_is_serious_maint() -> None:
    # bug obese-dihedral-ermine: the new serious maint binary. A test forbidding a state the
    # same diff's contract declares allowed is a SERIOUS (0.9) maintainability consequence,
    # undamped by churn like the other serious-maint binaries.
    assert impact_code({"forbids_contract_allowed_state": True}) == 0.72  # 0.9 * amp 0.8
    assert impact_code({"forbids_contract_allowed_state": True, "silent_failure": True}) == 0.9
    # undamped: cold == hot.
    assert impact_code({"forbids_contract_allowed_state": True}) == impact_code(
        {"forbids_contract_allowed_state": True, "churn90": 30}
    )


# ── impact_code: MODERATE maintainability lane — prod_impact-keyed (v4) ────────────────────
def test_moderate_maint_keyed_by_prod_impact() -> None:
    # v4: a moderate-maint binary is scaled by prod_impact (user-facing reach of the guarded
    # path): high/med=1.0, low=0.6, none=0.5. 0.6 tier × prod_impact_mult × trigger(common 1.0)
    # × amp 0.8 (not silent).
    assert impact_code({"contract_drift": True, "prod_impact": "none"}) == 0.24  # 0.6*0.5*1*0.8
    assert impact_code({"contract_drift": True, "prod_impact": "low"}) == 0.288  # 0.6*0.6*1*0.8
    assert impact_code({"contract_drift": True, "prod_impact": "medium"}) == 0.48  # 0.6*1*1*0.8
    assert impact_code({"contract_drift": True, "prod_impact": "high"}) == 0.48  # 0.6*1*1*0.8


def test_moderate_maint_default_prod_impact_is_none() -> None:
    # Absent prod_impact => none => 0.5 mult (an internal-edge maint finding stays advisory).
    assert impact_code({"hidden_invariant": True}) == impact_code(
        {"hidden_invariant": True, "prod_impact": "none"}
    )


def test_moderate_maint_fire_case_silent_blocks_arithmetic() -> None:
    # The fire class: a moderate-maint coverage/contract finding on a load-bearing guarded path
    # (prod_impact medium) that is SILENT reaches 0.6 — >= the packaged tests@0.54 threshold.
    assert (
        impact_code({"contract_drift": True, "prod_impact": "medium", "silent_failure": True})
        == 0.6
    )
    # Non-silent tops out at 0.48 by design (already-detected gaps stay advisory).
    assert impact_code({"contract_drift": True, "prod_impact": "medium"}) == 0.48


def test_moderate_maint_trigger_map_differs_from_prod() -> None:
    # v4: the moderate-maint lane reads its OWN trigger map {rare 0.75, sometimes/common 1.0}
    # from the same trigger_likelihood field — NOT the prod map {rare 0.25, sometimes 0.6}.
    base = {"contract_drift": True, "prod_impact": "medium"}
    assert impact_code({**base, "trigger_likelihood": "rare"}) == 0.36  # 0.6*1*0.75*0.8
    assert impact_code({**base, "trigger_likelihood": "sometimes"}) == 0.48  # 0.6*1*1.0*0.8
    assert impact_code({**base, "trigger_likelihood": "common"}) == 0.48  # sometimes == common


def test_moderate_maint_not_churn_scaled() -> None:
    # v4: the moderate lane is NOT churn-scaled (that moved to the debt amplifier). A hot
    # moderate finding scores exactly as a cold one.
    cold = impact_code({"reachable_path_without_automated_coverage": True, "prod_impact": "medium"})
    hot = impact_code(
        {
            "reachable_path_without_automated_coverage": True,
            "prod_impact": "medium",
            "churn90": 30,
        }
    )
    assert cold == hot == 0.48


# ── impact_code: DEBT lane — churn AMPLIFIER only (v4) ─────────────────────────────────────
def test_minor_maint_binary_cold() -> None:
    # v4: dead_code minor(0.3) * churn_amp(cold=1.0) * amp 0.8 -> 0.24 (was 0.12 under v3's
    # 0.5 freq floor — the amplifier can only INCREASE impact, never halve it).
    assert impact_code({"dead_code": True}) == 0.24


def test_debt_churn_zero_is_amplifier_one_not_half() -> None:
    # Operator invariant: churn may only INCREASE impact. churn=0 => mult EXACTLY 1.0, never 0.5.
    assert impact_code({"dead_code": True, "silent_failure": True}) == 0.3  # 0.3 * 1.0 * 1.0


def test_freq_mult_cold_vs_hot() -> None:
    # Debt-lane amplifier: a hot (churn90=30) debt finding scores higher than a cold one.
    cold = impact_code({"dead_code": True})
    hot = impact_code({"dead_code": True, "churn90": 30})
    assert cold == 0.24  # 0.3 * 1.0 * 0.8
    assert hot == 0.36  # 0.3 * 1.5 * 0.8


def test_freq_mult_clamped_at_30() -> None:
    a = impact_code({"dead_code": True, "churn90": 30})
    b = impact_code({"dead_code": True, "churn90": 999})
    assert a == b == 0.36


def test_freq_mult_bad_churn_falls_back() -> None:
    assert impact_code({"dead_code": True, "churn90": "oops"}) == impact_code({"dead_code": True})
    assert impact_code({"dead_code": True, "churn90": -5}) == impact_code({"dead_code": True})


def test_minor_alone_cannot_reach_block_zone() -> None:
    # A minor debt binary at its hottest/silent still stays below a 0.5 bar: 0.3 * 1.5 * 1.0.
    hot_silent = impact_code({"implicit_coupling": True, "churn90": 30, "silent_failure": True})
    assert hot_silent == 0.45
    assert hot_silent < 0.5


def test_trigger_likelihood_does_not_touch_debt_lane() -> None:
    # trigger_likelihood scales the prod and moderate-maint lanes, NOT the debt lane.
    assert impact_code({"dead_code": True, "trigger_likelihood": "rare"}) == impact_code(
        {"dead_code": True}
    )


# ── impact_code: detection amplifier ──────────────────────────────────────────────────────
def test_detection_amplifier_silent() -> None:
    assert impact_code({"data_loss_without_recovery": True, "silent_failure": True}) == 0.9
    assert impact_code({"data_loss_without_recovery": True, "escapes_automation": True}) == 0.9
    assert impact_code({"data_loss_without_recovery": True}) == 0.72  # neither -> x0.8


# ── impact_code: gated reversibility floor — CONSEQUENCE lanes only (v4) ───────────────────
def test_reversibility_floor_lifts_consequence_lane_defect() -> None:
    # v4: a MODERATE (consequence-lane) finding on a one-way-door surface is floored to 0.6.
    # contract_drift (prod_impact none) base = 0.6*0.5 = 0.3 > 0 => consequence base positive.
    assert impact_code({"contract_drift": True, "hard_to_reverse_surface": True}) == 0.6


def test_reversibility_floor_does_not_lift_debt_only_finding() -> None:
    # INVERTED from v3: a DEBT-only finding on a hard-to-reverse surface no longer floors —
    # the floor is restricted to the consequence lanes. implicit_coupling stays at its debt
    # score (0.3 * 1.0 * 0.8 = 0.24), NOT lifted to 0.6.
    assert impact_code({"implicit_coupling": True, "hard_to_reverse_surface": True}) == 0.24


def test_reversibility_floor_not_manufactured_for_clean_finding() -> None:
    # A finding with NO consequence binary on the same surface stays 0 (consequence_base == 0).
    assert impact_code({"hard_to_reverse_surface": True}) == 0.0


def test_reversibility_floor_does_not_lower_a_higher_score() -> None:
    # The floor is a MAX, never a cap: a serious silent finding stays 0.9, not pulled to 0.6.
    assert (
        impact_code(
            {
                "data_loss_without_recovery": True,
                "silent_failure": True,
                "hard_to_reverse_surface": True,
            }
        )
        == 0.9
    )


# ── impact_code: truthiness of consequence binaries ───────────────────────────────────────
def test_string_truthiness() -> None:
    assert impact_code({"dead_code": "true"}) == impact_code({"dead_code": True})
    assert impact_code({"dead_code": "yes"}) == impact_code({"dead_code": True})
    assert impact_code({"dead_code": "no"}) == 0.0
    assert impact_code({"dead_code": "false"}) == 0.0
    assert impact_code({"dead_code": ""}) == 0.0


def test_output_bounded_0_1() -> None:
    for attrs in ({}, {"data_loss_without_recovery": True, "silent_failure": True, "churn90": 99}):
        v = impact_code(attrs)
        assert 0.0 <= v <= 1.0


# ── per-gate impact_fn dispatch ───────────────────────────────────────────────────────────
def _verif(attrs: dict) -> dict:
    return {"binary": {}, "severity_attributes": attrs}


def test_pass3_decide_dispatches_impact_code() -> None:
    attrs = {"data_loss_without_recovery": True, "silent_failure": True}
    d = pass3_decide(_verif(attrs), block_threshold=0.7, impact_fn=impact_code)
    assert d["impact"] == impact_code(attrs)


def test_default_impact_fn_unchanged_for_absent_fn() -> None:
    # With NO impact_fn the mean `impact` is used, unchanged.
    attrs = {"prod_impact": "high", "debt_impact": "none"}
    d = pass3_decide(_verif(attrs), block_threshold=0.7)
    assert d["impact"] == impact(attrs)


def test_impact_fn_isolation_plan_vs_code() -> None:
    # The three impact models are independent: a plan-only attr dict scores 0 under impact_code
    # and a code-only attr dict scores 0 under impact_plan (no cross-contamination).
    assert impact_code({"ac_unverifiable": "high"}) == 0.0
    assert impact_plan({"data_loss_without_recovery": True}) == 0.0


def test_pass3_over_findings_threads_impact_code() -> None:
    findings = [{"criteria": ["x"]}, {"criteria": ["x"]}]
    verifs = {
        0: _verif({"dead_code": True}),
        1: _verif({"data_loss_without_recovery": True, "silent_failure": True}),
    }
    out = pass3_over_findings(
        findings, verifs, threshold_for=lambda _c: (0.7, True), impact_fn=impact_code
    )
    assert out[0]["impact"] == impact_code({"dead_code": True})
    assert out[1]["impact"] == 0.9


# ── DET-enrichment helpers (workflow_ops) ─────────────────────────────────────────────────
def test_det_helpers() -> None:
    from rebar.llm.code_review import workflow_ops as wo

    assert wo._file_from_location("src/a/b.py:42") == "src/a/b.py"
    assert wo._file_from_location("pyproject.toml") == "pyproject.toml"
    assert wo._file_from_location("") == ""
    # hard-to-reverse surfaces
    assert wo._hard_to_reverse_surface("pyproject.toml", set()) is True
    assert wo._hard_to_reverse_surface("a/b/setup.cfg", set()) is True
    assert wo._hard_to_reverse_surface("docs/CHANGELOG.md", set()) is True
    assert wo._hard_to_reverse_surface("db/x.sql", set()) is True
    assert wo._hard_to_reverse_surface("proto/x.proto", set()) is True
    assert wo._hard_to_reverse_surface("cfg/foo.schema.json", set()) is True
    assert wo._hard_to_reverse_surface("cfg/schema_v2.json", set()) is True
    assert wo._hard_to_reverse_surface("src/plain.py", set()) is False
    # a deletion is hard-to-reverse
    assert wo._hard_to_reverse_surface("src/plain.py", {"src/plain.py"}) is True


def test_deleted_paths_from_diff() -> None:
    from rebar.llm.code_review import workflow_ops as wo

    diff = (
        "diff --git a/src/gone.py b/src/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/src/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x = 1\n"
    )
    assert wo._deleted_paths_from_diff(diff) == {"src/gone.py"}
    assert wo._deleted_paths_from_diff("") == set()


def test_det_enrich_writes_to_verification_dict_not_finding() -> None:
    from rebar.llm.code_review import workflow_ops as wo

    findings = [{"location": "src/plain.py:1"}]
    verifs = {0: {"binary": {}, "severity_attributes": {"dead_code": True}}}
    wo._det_enrich_verifications(findings, verifs, diff_text="", repo_root=None)
    # DET signals land on the VERIFICATION dict (what impact_code reads), NOT the finding dict.
    assert verifs[0]["severity_attributes"]["churn90"] == 0  # repo_root None -> 0
    assert verifs[0]["severity_attributes"]["hard_to_reverse_surface"] is False
    assert "churn90" not in findings[0]


# ── new maint-lane binary reachable_path_without_automated_coverage (prod_impact-keyed) ────
def test_reachable_path_without_automated_coverage_scores_moderate_maint() -> None:
    # A reachable path with NO automated coverage is a MODERATE (0.6) maintainability
    # consequence, prod_impact-keyed. Default prod_impact none: 0.6 * 0.5 * 1.0 * 0.8 = 0.24.
    assert impact_code({"reachable_path_without_automated_coverage": True}) == 0.24


def test_reachable_path_load_bearing_silent_reaches_block_zone() -> None:
    # v4: when the guarded path is load-bearing (prod_impact medium) and the gap is silent,
    # a coverage finding reaches 0.6 — the intended blocking class.
    assert (
        impact_code(
            {
                "reachable_path_without_automated_coverage": True,
                "prod_impact": "medium",
                "silent_failure": True,
            }
        )
        == 0.6
    )


def test_529_shaped_untested_degradation_now_scores_nonzero() -> None:
    # #529's advisory ("new degrade behavior has no test") scored 0.0 under code-v2; the maint
    # binary makes it reachable-to-block (> 0) under v3/v4.
    assert impact_code({"reachable_path_without_automated_coverage": True}) > 0.0


def test_binary_false_contributes_nothing() -> None:
    # Abstain-safe: an explicit False (or absence) must NOT inflate impact.
    assert impact_code({"reachable_path_without_automated_coverage": False}) == 0.0


def test_new_binary_is_moderate_below_serious() -> None:
    # MODERATE (0.24 default) must rank strictly below a SERIOUS maint binary (0.72 not-silent).
    new = impact_code({"reachable_path_without_automated_coverage": True})
    serious = impact_code({"safety_net_removal_without_replacement": True})
    assert new < serious


# ── prod-lane byte-compat + debt rescale (v3 -> v4) ───────────────────────────────────────
def test_prod_lane_scores_unchanged_from_v3() -> None:
    # The PRODUCTION lane is untouched by v4: correctness findings score exactly as before.
    assert impact_code({}) == 0.0
    assert impact_code({"data_loss_without_recovery": True}) == 0.72
    assert impact_code({"capability_degraded": True}) == 0.48


def test_debt_lane_rescaled_amplifier_only() -> None:
    # v4 debt rescale: cold debt rises 0.12 -> 0.24 (amplifier floor 1.0, not 0.5).
    assert impact_code({"dead_code": True}) == 0.24
    assert impact_code({"implicit_coupling": True}) == 0.24


# ── code-v5: removed-public-symbol boost (ticket 5452-3077-b34a-4157) ──────────────────────
def test_removed_public_symbol_unmanaged_boosts_to_serious() -> None:
    # proposal-3: a removed PUBLIC export with NO version/deprecation signal is the same
    # consequence as an unversioned published contract break — serious 0.9 × amp 0.8 = 0.72.
    assert impact_code({"removed_public_symbol": True}) == 0.72
    assert impact_code({"removed_public_symbol": True, "silent_failure": True}) == 0.9


def test_removed_public_symbol_managed_removal_never_trips() -> None:
    # a2: a MANAGED removal (version/deprecation signal present) does not trip the boost.
    assert impact_code({"removed_public_symbol": True, "version_signal_present": True}) == 0.0


def test_version_signal_alone_abstains() -> None:
    # version_signal_present is a GATE input only — alone it contributes nothing, and it never
    # suppresses a score another lane already earned.
    assert impact_code({"version_signal_present": True}) == 0.0
    base = {"capability_degraded": True}
    assert impact_code({**base, "version_signal_present": True}) == impact_code(base)
    managed_break = {
        "unversioned_published_contract_break": True,
        "version_signal_present": True,
    }
    assert impact_code(managed_break) == impact_code({"unversioned_published_contract_break": True})


def test_citric_preregal_ladybird_replay_clears_flips() -> None:
    # The escaped FN: [regression, deletion-impact, tests] finding naming the removed public
    # API rebar.llm.review_ticket + a broken external test was labeled capability_degraded +
    # reachable_path_without_automated_coverage with trigger rare — impact 0.30-shaped, below
    # deletion-impact@0.60 / api-compat@0.51. With the export-keyed removed-public-symbol
    # sub-answer set (and no version signal) it clears both flips.
    citric = {
        "capability_degraded": True,
        "reachable_path_without_automated_coverage": True,
        "trigger_likelihood": "rare",
    }
    assert impact_code(citric) < 0.51  # the pre-boost mislabeled shape stays low
    assert impact_code({**citric, "removed_public_symbol": True}) >= 0.60


def test_citric_replay_blocks_through_packaged_routing() -> None:
    # END-TO-END through the REAL packaged routing: the citric-shaped finding tagged
    # [regression, deletion-impact] with validity 1.0 now BLOCKS (priority 0.72 ≥ 0.54/0.60).
    from rebar.llm.code_review import registry as reg

    findings = [
        {
            "id": "0",
            "finding": "removed public API rebar.llm.review_ticket breaks a named external test",
            "criteria": ["regression", "deletion-impact"],
        }
    ]
    verifs = {
        0: {
            "binary": {
                "is_verifiable": "yes",
                "evidence_entails_finding": "yes",
                "path_reachable": "yes",
                "impact_follows_necessarily": "yes",
                "no_viable_alternative_explanation": "yes",
                "no_existing_mitigation": "yes",
                "severity_claim_justified": "yes",
            },
            "severity_attributes": {
                "capability_degraded": True,
                "reachable_path_without_automated_coverage": True,
                "trigger_likelihood": "rare",
                "removed_public_symbol": True,
            },
        }
    }
    out = pass3_over_findings(
        findings, verifs, threshold_for=reg.threshold_for, impact_fn=impact_code
    )
    assert out[0]["impact"] == 0.72
    assert out[0]["priority"] == 0.72
    assert out[0]["decision"] == "block"


def test_boost_is_amplify_only_never_lowers() -> None:
    # The boost can only RAISE impact_base: a finding already at 0.9 stays 0.9 whether or not
    # the removal sub-answers are present (managed or unmanaged).
    base = {"data_loss_without_recovery": True, "silent_failure": True}
    assert impact_code(base) == 0.9
    assert impact_code({**base, "removed_public_symbol": True}) == 0.9
    assert (
        impact_code({**base, "removed_public_symbol": True, "version_signal_present": True}) == 0.9
    )


# ── labeled-fixture calibration: HIGH vs NIT separation under v4 ───────────────────────────
def _load_fixture() -> list[dict]:
    return [json.loads(line) for line in _FIXTURE.read_text().splitlines() if line.strip()]


def test_fixture_present_and_labeled() -> None:
    rows = _load_fixture()
    assert len(rows) >= 20
    labels = {r["label"] for r in rows}
    assert labels == {"HIGH", "NIT"}


def _median_gap(fn) -> float:
    rows = _load_fixture()
    high = [fn(r["severity_attributes"]) for r in rows if r["label"] == "HIGH"]
    nit = [fn(r["severity_attributes"]) for r in rows if r["label"] == "NIT"]
    return statistics.median(high) - statistics.median(nit)


def test_impact_code_separates_high_from_nit() -> None:
    # v4 re-verification over the labeled fixture: median HIGH 0.90, NIT 0.24, separation 0.66.
    rows = _load_fixture()
    high = [impact_code(r["severity_attributes"]) for r in rows if r["label"] == "HIGH"]
    nit = [impact_code(r["severity_attributes"]) for r in rows if r["label"] == "NIT"]
    m_high, m_nit = statistics.median(high), statistics.median(nit)
    assert m_high == 0.90
    assert m_nit == 0.24
    assert m_nit < 0.30
    assert (m_high - m_nit) > 0.30


def test_impact_code_gap_beats_old_mean_gap() -> None:
    # impact_code's HIGH<->NIT median gap must be STRICTLY GREATER than the old kernel mean
    # `impact`'s gap on the SAME labeled set.
    assert _median_gap(impact_code) > _median_gap(impact)
