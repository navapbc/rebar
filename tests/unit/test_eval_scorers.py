"""Deterministic eval-scorer registry + strict spec validation (epic 6f2d /
WS-EVAL-EXISTING). All offline, no model — the scorers are pure functions and the
strict validator is stdlib + the registry."""

from __future__ import annotations

import glob

import yaml

from rebar.llm.evals import eval as ev
from rebar.llm.evals import eval_scorers as sc

# ── the registry covers every packaged spec's scorer names ─────────────────────


def test_every_packaged_deterministic_scorer_is_registered() -> None:
    known = sc.known_scorer_names()
    used: set[str] = set()
    for p in glob.glob("src/rebar/llm/eval_specs/*.eval.yaml"):
        spec = yaml.safe_load(open(p).read())
        for s in spec.get("scorers", []):
            if s.get("type") == "deterministic":
                used.add(s["name"])
    assert used, "no deterministic scorers found in packaged specs"
    missing = used - known
    assert not missing, f"packaged specs name unregistered scorers: {sorted(missing)}"


def test_score_dispatch_and_unknown_raises() -> None:
    out = {"findings": [{"severity": "high", "dimension": "bug", "detail": "x"}]}
    assert sc.score("recall_on_seeded_defects", {"expect": "finding"}, out).passed is True
    try:
        sc.score("no_such_scorer", {}, out)
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown scorer name must raise KeyError")


# ── recall / no-fire archetypes ────────────────────────────────────────────────

_FINDING = {"findings": [{"severity": "high", "dimension": "bug", "detail": "d"}]}
_EMPTY = {"findings": []}


def test_recall_fires_on_should_fire_cases() -> None:
    assert sc.score("recall_on_seeded_defects", {"expect": "finding"}, _FINDING).passed is True
    # missed defect → fail
    r = sc.score("recall_on_seeded_defects", {"expect": "finding"}, _EMPTY)
    assert r.applicable is True and r.passed is False
    # not a should-fire case → not applicable (excluded from recall)
    assert sc.score("recall_on_seeded_defects", {"expect": "pass"}, _EMPTY).applicable is False


def test_no_fire_on_good_cases() -> None:
    assert sc.score("no_fire_on_good_cases", {"expect": "pass"}, _EMPTY).passed is True
    bad = sc.score("no_fire_on_good_cases", {"expect": "pass"}, _FINDING)
    assert bad.applicable is True and bad.passed is False
    assert sc.score("no_fire_on_good_cases", {"expect": "finding"}, _FINDING).applicable is False


def test_completion_verdict_fires_on_fail() -> None:
    fail = {
        "verdict": "FAIL",
        "findings": [
            {
                "severity": "high",
                "dimension": "ac",
                "detail": "x",
                "citations": [{"kind": "file", "path": "a.py", "line_start": 1}],
            }
        ],
    }
    ok = {"verdict": "PASS", "findings": []}
    assert sc.score("recall_on_incomplete", {"expect": "fail"}, fail).passed is True
    assert sc.score("no_false_fail_on_complete", {"expect": "pass"}, ok).passed is True


# ── schema / contract scorers ──────────────────────────────────────────────────


def test_emits_valid_review_result() -> None:
    assert sc.score("emits_valid_review_result", {}, _FINDING).passed is True
    bad = sc.score("emits_valid_review_result", {}, {"findings": [{"detail": "no severity"}]})
    assert bad.passed is False


def test_emits_valid_verdict_contract() -> None:
    good = {
        "verdict": "FAIL",
        "findings": [
            {
                "severity": "high",
                "dimension": "ac",
                "detail": "x",
                "citations": [{"kind": "file", "path": "a.py", "line_start": 2}],
            }
        ],
    }
    assert sc.score("emits_valid_verdict", {}, good).passed is True
    # FAIL with no findings violates FAIL<=>findings
    assert sc.score("emits_valid_verdict", {}, {"verdict": "FAIL", "findings": []}).passed is False
    # FAIL finding with no citation fails the source-citation contract
    nocite = {
        "verdict": "FAIL",
        "findings": [{"severity": "high", "dimension": "ac", "detail": "x"}],
    }
    assert sc.score("emits_valid_verdict", {}, nocite).passed is False
    # bad verdict value
    assert sc.score("emits_valid_verdict", {}, {"verdict": "MAYBE"}).passed is False


def test_cites_real_paths() -> None:
    cited = {
        "findings": [
            {
                "severity": "high",
                "dimension": "bug",
                "detail": "d",
                "citations": [{"kind": "file", "path": "src/x.py", "line_start": 3}],
            }
        ]
    }
    assert sc.score("cites_real_paths", {}, cited).passed is True
    # a downgraded (source-only) citation does not count as a real file citation
    downgraded = {
        "findings": [
            {
                "severity": "high",
                "dimension": "bug",
                "detail": "d",
                "citations": [{"kind": "source", "description": "unresolved file citation: x"}],
            }
        ]
    }
    assert sc.score("cites_real_paths", {}, downgraded).passed is False
    # no findings → not applicable
    assert sc.score("cites_real_paths", {}, _EMPTY).applicable is False


# ── verifier discrimination scorers ────────────────────────────────────────────


def test_discriminates_true_from_false() -> None:
    true_case = {"expect": "high_validity", "kind": "true"}
    false_case = {"expect": "low_validity", "kind": "false"}
    assert sc.score("discriminates_true_from_false", true_case, {"validity": 0.9}).passed is True
    assert sc.score("discriminates_true_from_false", false_case, {"validity": 0.1}).passed is True
    # a false finding graded high → fails discrimination
    assert sc.score("discriminates_true_from_false", false_case, {"validity": 0.8}).passed is False


def test_no_sycophancy_on_real_defects() -> None:
    real = {"expect": "high_validity", "kind": "true"}
    assert sc.score("no_sycophancy_on_real_defects", real, {"validity": 0.9}).passed is True
    # dismissing a real defect (low validity) is sycophancy → fail
    assert sc.score("no_sycophancy_on_real_defects", real, {"validity": 0.2}).passed is False
    # not a real-defect case → not applicable
    false_case = {"expect": "low_validity", "kind": "false"}
    na = sc.score("no_sycophancy_on_real_defects", false_case, {"validity": 0.1})
    assert na.applicable is False


def test_validity_extraction_from_labels() -> None:
    assert sc._validity({"verdict": "valid"}) == 1.0
    assert sc._validity({"label": "invalid"}) == 0.0
    assert sc._validity({"validity": 0.7}) == 0.7
    assert sc._validity({}) is None


# ── strict spec validation ─────────────────────────────────────────────────────


def _base_spec(**over) -> dict:
    spec = {
        "prompt": "p",
        "model": "anthropic:claude-opus-4-8",
        "epochs": 3,
        "gate": "at_least(2)",
        "coverage_threshold": 0.8,
        "scorers": [{"type": "deterministic", "name": "emits_valid_review_result"}],
        "dataset": [
            {"id": "a", "expect": "finding", "input": "bad plan"},
            {"id": "b", "expect": "pass", "input": "good plan"},
        ],
        "gold_set": [{"input": "x", "label": "useful"}],
    }
    spec.update(over)
    return spec


def test_strict_accepts_well_formed_spec() -> None:
    assert ev.validate_eval_spec(_base_spec(), strict=True) == []


def test_strict_rejects_unregistered_scorer() -> None:
    spec = _base_spec(scorers=[{"type": "deterministic", "name": "made_up_scorer"}])
    errs = ev.validate_eval_spec(spec, strict=True)
    assert any("not a registered scorer" in e for e in errs), errs
    # lenient (default) does NOT reject — backward compatible
    assert ev.validate_eval_spec(spec) == []


def test_strict_requires_dataset_and_gold() -> None:
    assert any("dataset" in e for e in ev.validate_eval_spec(_base_spec(dataset=[]), strict=True))
    assert any("gold_set" in e for e in ev.validate_eval_spec(_base_spec(gold_set=[]), strict=True))


def test_strict_requires_balance() -> None:
    one_sided = _base_spec(dataset=[{"id": "a", "expect": "finding", "input": "x"}])
    assert any("balanced" in e for e in ev.validate_eval_spec(one_sided, strict=True))


def test_strict_catches_missing_payload_and_dupe_id() -> None:
    spec = _base_spec(
        dataset=[
            {"id": "a", "expect": "finding"},  # no payload
            {"id": "a", "expect": "pass", "input": "y"},  # dup id
        ]
    )
    errs = ev.validate_eval_spec(spec, strict=True)
    assert any("payload" in e for e in errs), errs
    assert any("duplicate" in e for e in errs), errs


def test_three_plan_review_specs_pass_strict() -> None:
    for name in ("plan-review-finder", "plan-review-verifier", "plan-review-isf-finder"):
        spec = yaml.safe_load(open(f"src/rebar/llm/eval_specs/{name}.eval.yaml").read())
        assert ev.validate_eval_spec(spec, strict=True) == [], name


# ── 74d9: completion-verifier enumerates ALL unmet criteria (happy path) ────────
# The scorer `enumerates_all_unmet_criteria` proves a FAIL verdict emits a distinct
# finding for EVERY criterion the case annotates as unmet (anti-ratchet). Happy path:
# it reads `expected_unmet_criteria` (list of substrings) off the case and requires a
# bijective match against the findings' `criterion` fields.

_TWO_UNMET_CASE = {"expect": "fail", "expected_unmet_criteria": ["AC1", "AC2"]}
_TWO = _TWO_UNMET_CASE


def test_enumerates_all_unmet_criteria_both_present() -> None:
    out = {"findings": [{"criterion": "AC1 unmet"}, {"criterion": "AC2 unmet"}]}
    r = sc.score("enumerates_all_unmet_criteria", _TWO_UNMET_CASE, out)
    assert r.applicable is True and r.passed is True


def test_enumerates_all_unmet_criteria_one_missing_fails() -> None:
    out = {"findings": [{"criterion": "AC1 unmet"}]}
    r = sc.score("enumerates_all_unmet_criteria", _TWO_UNMET_CASE, out)
    assert r.applicable is True and r.passed is False


def test_abstains_when_no_expected_criteria() -> None:
    # No annotation → not applicable (the scorer only judges annotated cases).
    r = sc.score("enumerates_all_unmet_criteria", {"expect": "fail"}, {"findings": []})
    assert r.applicable is False


def test_abstains_on_empty_expected_list() -> None:
    r = sc.score(
        "enumerates_all_unmet_criteria",
        {"expect": "fail", "expected_unmet_criteria": []},
        {"findings": [{"criterion": "AC1"}]},
    )
    assert r.applicable is False


def test_bijective_one_finding_cannot_cover_two_criteria() -> None:
    # A single finding mentioning BOTH substrings must NOT satisfy two distinct criteria
    # (anti-ratchet requires one finding PER unmet criterion).
    out = {"findings": [{"criterion": "AC1 and AC2 both unmet"}]}
    r = sc.score("enumerates_all_unmet_criteria", _TWO, out)
    assert r.applicable is True and r.passed is False


def test_case_insensitive_substring_match() -> None:
    out = {"findings": [{"criterion": "ac1 gap"}, {"criterion": "the AC2 item"}]}
    r = sc.score("enumerates_all_unmet_criteria", _TWO, out)
    assert r.applicable is True and r.passed is True


def test_extra_findings_do_not_break_a_full_cover() -> None:
    out = {"findings": [{"criterion": "AC1"}, {"criterion": "AC2"}, {"criterion": "AC9 noise"}]}
    r = sc.score("enumerates_all_unmet_criteria", _TWO, out)
    assert r.applicable is True and r.passed is True
