"""Deterministic eval-scorer registry + strict spec validation (epic 6f2d /
WS-EVAL-EXISTING). All offline, no model — the scorers are pure functions and the
strict validator is stdlib + the registry."""

from __future__ import annotations

import glob
import subprocess
import sys

import pytest
import yaml

from rebar.llm.evals import eval as ev
from rebar.llm.evals import eval_scorers as sc

# ── the registry covers every packaged spec's scorer names ─────────────────────


def test_every_packaged_deterministic_scorer_is_registered() -> None:
    known = sc.known_scorer_names()
    used: set[str] = set()
    for p in glob.glob("src/rebar/llm/eval_specs/*.eval.yaml"):
        with open(p, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh.read())
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


def test_every_packaged_spec_passes_strict_validation() -> None:
    """Validate every packaged spec in the default test suite.

    All specs must be lenient-clean. Dataset-bearing specs must also pass strict
    scorer, balance, and nonempty-gold-set checks. Globbing covers new specs.
    """
    specs = glob.glob("src/rebar/llm/eval_specs/*.eval.yaml")
    assert specs, "no packaged eval specs found"
    for p in specs:
        with open(p, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh.read())
        assert ev.validate_eval_spec(spec) == [], p
        if spec.get("dataset"):
            assert ev.validate_eval_spec(spec, strict=True) == [], p


# Require a bijection between expected unmet criteria and emitted finding criteria.

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


# ── emits_valid_findings scores the plan_review_findings contract (bug newborn-advanced-hoopoe) ──

_CRITERION_ID_SPECS = {
    "plan-review-G7",
    "plan-review-ac-process-gate",
    "plan-review-asserted-capability",
    "plan-review-decomp-shape",
    "plan-review-evidence-kind",
    "plan-review-necessity",
    "plan-review-no-file-impact",
}


def _criterion_id_specs_using_emits_valid_findings() -> dict[str, dict]:
    from rebar.llm.evals import eval_solver

    found: dict[str, dict] = {}
    for p in sorted(glob.glob("src/rebar/llm/eval_specs/*.eval.yaml")):
        with open(p, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh.read())
        names = {s.get("name") for s in spec.get("scorers", [])}
        prompt = spec["prompt"]
        if "emits_valid_findings" in names and eval_solver._criterion_id(prompt, ".") is not None:
            found[prompt] = spec
    return found


def test_the_criterion_id_specs_using_emits_valid_findings_are_the_expected_set() -> None:
    assert set(_criterion_id_specs_using_emits_valid_findings()) == _CRITERION_ID_SPECS


@pytest.mark.parametrize("prompt_id", sorted(_CRITERION_ID_SPECS))
def test_a_fired_criterion_arm_finding_is_a_valid_plan_review_finding(prompt_id) -> None:
    from rebar.llm.evals import eval_solver
    from rebar.llm.runner import FakeRunner

    spec = _criterion_id_specs_using_emits_valid_findings()[prompt_id]
    case = next(c for c in spec["dataset"] if c.get("expect") == "finding")
    cid = eval_solver._criterion_id(prompt_id, ".")
    fake = FakeRunner(findings=[{"finding": "gap", "criteria": [cid], "evidence": ["q"]}])
    out = eval_solver.run_case(prompt_id, case, runner=fake, repo_root=".")
    assert out["findings"], "the fake finding must survive the solver"
    res = sc.score("emits_valid_findings", case, out)
    assert res.passed is True, res.detail
    assert "dimension" not in res.detail
    assert sc.score("emits_valid_findings", case, {"findings": []}).passed is True


def test_emits_valid_findings_registers_its_contract_in_a_fresh_interpreter() -> None:
    # `import rebar` already imports plan_review.passes (which registers the contract), so the
    # fresh interpreter UNREGISTERS it first: the scorer must then register it itself rather
    # than fall back to the review_result model, which would reject the missing `dimension`.
    script = (
        "from rebar.llm import contracts\n"
        "from rebar.llm.evals import eval_scorers as sc\n"
        "contracts._CONTRACTS.pop('plan_review_findings')\n"
        "r = sc.score('emits_valid_findings', {}, "
        "{'findings': [{'finding': 'x', 'criteria': ['G7']}]})\n"
        "assert r.passed, r.detail\n"
        "assert 'plan_review_findings' in contracts._CONTRACTS\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "out",
    [
        {"findings": [{"severity": "high", "dimension": "x", "detail": "d"}]},
        {"findings": [{"finding": "  ", "criteria": ["G7"]}]},
        {"findings": "not a list"},
        {},
        "not a dict",
    ],
)
def test_emits_valid_findings_rejects_malformed_plan_review_output(out) -> None:
    assert sc.score("emits_valid_findings", {}, out).passed is False


def test_attributes_g3_vs_g4_credits_a_container_arm_finding() -> None:
    from rebar.llm.evals import eval as ev
    from rebar.llm.evals import eval_solver
    from rebar.llm.runner import FakeRunner

    spec = ev.load_eval_spec("plan-review-container")
    case = next(
        c for c in spec["dataset"] if c.get("criterion") == "G3" and c.get("expect") == "finding"
    )
    fake = FakeRunner(findings=[{"finding": "gap", "criteria": ["G3"], "evidence": ["q"]}])
    out = eval_solver.run_case("G3", case, runner=fake, repo_root=".")
    assert out["findings"]
    assert sc.score("attributes_g3_vs_g4", case, out).passed is True


@pytest.mark.parametrize(
    ("case_criterion", "finding_criteria", "credited"),
    [
        ("G3", ["G4"], False),
        ("G3", ["G3", "G4"], True),
        ("G4", ["G3", "G4"], True),
        ("G3", ["G33"], False),
        ("G3", ["g3"], True),
    ],
)
def test_attributes_g3_vs_g4_matches_the_criteria_list_exactly(
    case_criterion, finding_criteria, credited
) -> None:
    case = {"expect": "finding", "criterion": case_criterion}
    out = {"findings": [{"finding": "x", "criteria": finding_criteria}]}
    assert sc.score("attributes_g3_vs_g4", case, out).passed is credited
