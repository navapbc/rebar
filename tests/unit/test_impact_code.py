"""Code-review impact redesign (story albite-lazy-barb).

Covers ``decide.impact_code`` (two-lane tier-tagged severity-first MAX with a per-lane
likelihood/frequency multiplier, a detection amplifier, and a gated reversibility floor), the
per-gate ``impact_fn`` dispatch, the DET-enrichment helpers in ``code_review.workflow_ops``, and
the labeled-fixture calibration (HIGH vs NIT separation).

Proving command:
    .venv/bin/pytest tests/unit/test_impact_code.py -v
    .venv/bin/pytest tests/unit/test_impact_code.py tests/unit/test_impact_plan.py -v
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
    # Only the base severity attributes present (no consequence binaries) -> still 0.
    assert (
        impact_code({"prod_impact": "high", "blast_radius": "system", "reversibility": "hard"})
        == 0.0
    )


# ── impact_code: tiers within a lane, MAX (no dilution / no compounding) ───────────────────
def test_serious_prod_binary_common_notsilent() -> None:
    # serious(0.9) * common(1.0) prod_lane; amp 0.8 (not silent) -> 0.72.
    assert impact_code({"data_loss_without_recovery": True}) == 0.72


def test_moderate_prod_binary() -> None:
    # capability_degraded moderate(0.6) * common(1.0) * amp 0.8 -> 0.48.
    assert impact_code({"capability_degraded": True}) == 0.48


def test_minor_maint_binary_cold() -> None:
    # dead_code minor(0.3) * freq_mult(cold=0.5) * amp 0.8 -> 0.12.
    assert impact_code({"dead_code": True}) == 0.12


def test_lane_max_not_sum_no_compounding() -> None:
    # Two serious prod binaries do not compound: MAX(0.9, 0.9) = 0.9, not 1.8.
    both = impact_code(
        {"data_loss_without_recovery": True, "silent_wrong_feeding_a_decision": True}
    )
    one = impact_code({"data_loss_without_recovery": True})
    assert both == one == 0.72


def test_minor_alone_cannot_reach_block_zone() -> None:
    # A minor binary at its hottest/silent still stays well below a 0.7 block bar.
    hot_silent = impact_code({"implicit_coupling": True, "churn90": 30, "silent_failure": True})
    assert hot_silent < 0.5  # 0.3 * 1.0(freq) * 1.0(amp) = 0.30


# ── impact_code: trigger-likelihood multiplier (production lane only) ──────────────────────
def test_trigger_likelihood_scales_prod_lane() -> None:
    base = {"data_loss_without_recovery": True}
    assert impact_code({**base, "trigger_likelihood": "common"}) == 0.72
    assert impact_code({**base, "trigger_likelihood": "sometimes"}) == round(0.9 * 0.6 * 0.8, 4)
    assert impact_code({**base, "trigger_likelihood": "rare"}) == round(0.9 * 0.25 * 0.8, 4)


def test_trigger_likelihood_absent_is_common() -> None:
    # Absent trigger_likelihood must NOT dampen a serious correctness finding (defaults common).
    assert impact_code({"data_loss_without_recovery": True}) == impact_code(
        {"data_loss_without_recovery": True, "trigger_likelihood": "common"}
    )


def test_trigger_likelihood_does_not_touch_maint_lane() -> None:
    # A maintainability binary is unaffected by trigger_likelihood (that scales the prod lane).
    assert impact_code({"dead_code": True, "trigger_likelihood": "rare"}) == impact_code(
        {"dead_code": True}
    )


# ── impact_code: change-frequency multiplier (maintainability lane only) ───────────────────
def test_freq_mult_cold_vs_hot() -> None:
    serious = {"safety_net_removal_without_replacement": True}
    assert impact_code(serious) == round(0.9 * 0.5 * 0.8, 4)  # churn 0 -> freq 0.5 -> 0.36
    assert impact_code({**serious, "churn90": 30}) == round(0.9 * 1.0 * 0.8, 4)  # 0.72


def test_freq_mult_clamped_at_30() -> None:
    a = impact_code({"safety_net_removal_without_replacement": True, "churn90": 30})
    b = impact_code({"safety_net_removal_without_replacement": True, "churn90": 999})
    assert a == b


def test_freq_mult_bad_churn_falls_back() -> None:
    assert impact_code({"dead_code": True, "churn90": "oops"}) == impact_code({"dead_code": True})
    assert impact_code({"dead_code": True, "churn90": -5}) == impact_code({"dead_code": True})


# ── impact_code: detection amplifier ──────────────────────────────────────────────────────
def test_detection_amplifier_silent() -> None:
    assert impact_code({"data_loss_without_recovery": True, "silent_failure": True}) == 0.9
    assert impact_code({"data_loss_without_recovery": True, "escapes_automation": True}) == 0.9
    assert impact_code({"data_loss_without_recovery": True}) == 0.72  # neither -> x0.8


# ── impact_code: gated reversibility floor ────────────────────────────────────────────────
def test_reversibility_floor_lifts_genuine_defect() -> None:
    # A minor finding on a one-way-door surface is floored to 0.6 (impact_base > 0).
    assert impact_code({"implicit_coupling": True, "hard_to_reverse_surface": True}) == 0.6


def test_reversibility_floor_not_manufactured_for_clean_finding() -> None:
    # A finding with NO consequence binary on the same surface stays 0 (impact_base == 0 gate).
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


def test_default_impact_fn_unchanged_for_code_review_absent_fn() -> None:
    # With NO impact_fn (the historical code-review path), the mean `impact` is used, unchanged.
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


# ── labeled-fixture calibration (AC4): HIGH vs NIT separation ──────────────────────────────
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
    rows = _load_fixture()
    high = [impact_code(r["severity_attributes"]) for r in rows if r["label"] == "HIGH"]
    nit = [impact_code(r["severity_attributes"]) for r in rows if r["label"] == "NIT"]
    m_high, m_nit = statistics.median(high), statistics.median(nit)
    assert m_high > m_nit
    assert m_nit < 0.30
    assert (m_high - m_nit) > 0.30


def test_impact_code_gap_beats_old_mean_gap() -> None:
    # AC4: impact_code's HIGH↔NIT median gap must be STRICTLY GREATER than the old kernel mean
    # `impact`'s gap on the SAME labeled set — the old mean averages the consequence binaries it
    # cannot see down toward 0 and CANNOT separate landmines from nits (the 0.33 attribute floor
    # is dropped; separation now comes from the two-lane tier model).
    assert _median_gap(impact_code) > _median_gap(impact)


# ── f32e: new maint-lane binary reachable_path_without_automated_coverage ────────
def test_reachable_path_without_automated_coverage_scores_moderate_maint() -> None:
    # A change that introduces/unmasks a reachable path with NO automated coverage is a
    # MODERATE (0.6) maintainability consequence. Cold (no churn) × detection amp 0.8:
    # 0.6 × 0.5 × 0.8 = 0.24 — mirrors the other moderate maint binaries (contract_drift).
    assert impact_code({"reachable_path_without_automated_coverage": True}) == 0.24


def test_529_shaped_untested_degradation_now_scores_nonzero() -> None:
    # #529's advisory ("new degrade behavior has no test") scored impact 0.0 under code-v2
    # and went red on main. Under code-v3 the new binary makes it reachable-to-block (> 0).
    assert impact_code({"reachable_path_without_automated_coverage": True}) > 0.0


# f32e held-out edge coverage (abstain / byte-compat / tier-ordering / churn):


def test_binary_false_contributes_nothing() -> None:
    # Abstain-safe: an explicit False (or absence) must NOT inflate impact.
    assert impact_code({"reachable_path_without_automated_coverage": False}) == 0.0


def test_old_sidecars_byte_unchanged() -> None:
    # code-v2 findings that never carried the new binary score EXACTLY as before.
    assert impact_code({}) == 0.0
    assert impact_code({"data_loss_without_recovery": True}) == 0.72
    assert impact_code({"dead_code": True}) == 0.12


def test_new_binary_is_moderate_below_serious() -> None:
    # MODERATE (0.24 cold) must rank strictly below a SERIOUS maint binary.
    new = impact_code({"reachable_path_without_automated_coverage": True})
    serious = impact_code({"safety_net_removal_without_replacement": True})
    assert new < serious


def test_new_binary_scales_with_churn() -> None:
    # Maint-lane freq multiplier: a hot (churn90=30) reachable-uncovered path scores higher
    # than a cold one (0.6 x 1.0 x 0.8 = 0.48 vs 0.6 x 0.5 x 0.8 = 0.24).
    cold = impact_code({"reachable_path_without_automated_coverage": True})
    hot = impact_code({"reachable_path_without_automated_coverage": True, "churn90": 30})
    assert hot > cold
