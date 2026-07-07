"""Standing eval-suite tests for the plan-review prompts (epic 5fd2 / child 7284).

These validate the SHIPPED plan-review eval specs offline (no model, no network): each
spec parses + passes `validate_eval_spec`, and the labeled datasets carry the three
case types (recall / false-accept / false-fire) the standing suite requires — including
the Pass-2 DISCRIMINATION pairs (child acc1) and the ISF recall/justified-descope cases
(child 681b), seeded from the real observed false-fire taxonomy. The live model run is
gated behind the `[eval]` extra in the eval CI; the gold subset is human-adjudicated
(these seed labels stand in pending that adjudication — see eval_specs/README.plan-review.md).
"""

from __future__ import annotations

import pytest

from rebar.llm.evals import eval as E

PLAN_REVIEW_SPECS = (
    "plan-review-finder",
    "plan-review-verifier",
    "plan-review-isf-finder",
    "plan-review-container",  # G3/G4 container fidelity (story da34)
)


@pytest.mark.parametrize("prompt_id", PLAN_REVIEW_SPECS)
def test_eval_spec_validates(prompt_id: str) -> None:
    spec = E.load_eval_spec(prompt_id)
    assert E.validate_eval_spec(spec) == []
    # A gating deterministic scorer + a non-gating cross-family llm-judge.
    types = {s["type"] for s in spec["scorers"]}
    assert "deterministic" in types and "llm-judge" in types


@pytest.mark.parametrize("prompt_id", PLAN_REVIEW_SPECS)
def test_dataset_has_recall_and_negative_cases(prompt_id: str) -> None:
    ds = E.load_eval_spec(prompt_id).get("dataset", [])
    assert ds, f"{prompt_id} has no labeled dataset"
    recall = [c for c in ds if c.get("expect") in ("finding", "high_validity")]
    negative = [c for c in ds if c.get("expect") in ("pass", "low_validity")]
    assert recall, f"{prompt_id} needs recall (bad→finding) cases"
    assert negative, f"{prompt_id} needs false-fire/false-accept (good→pass) cases"


def test_novelty_spec_is_strict_clean_and_balanced() -> None:
    # child 150b: the SEPARATE novelty sub-call's eval lives in its OWN spec (keyed to the
    # plan-review-novelty prompt, NOT the verifier spec — the discriminates_novelty scorer
    # would otherwise score the wrong artifact). Guard it in CI: strict-clean (catches an
    # unbalanced novelty axis, an emptied gold_set, or a renamed/unregistered scorer), the
    # discriminator present, both novelty poles, and >= 3 carryover/novel pairs.
    spec = E.load_eval_spec("plan-review-novelty")
    assert E.validate_eval_spec(spec, strict=True) == []
    names = {s["name"] for s in spec["scorers"]}
    assert "discriminates_novelty" in names
    ds = spec.get("dataset", [])
    expects = {c.get("expect") for c in ds}
    assert {"high_novelty", "low_novelty"} <= expects, "novelty axis needs both poles"
    pairs = {c.get("pair") for c in ds if c.get("pair")}
    assert len(pairs) >= 3, "150b AC needs >= 3 labeled carryover/novel pairs"
    kinds = {c.get("kind") for c in ds}
    assert {"carryover", "novel"} <= kinds
    assert spec.get("gold_set"), "novelty spec needs a gold_set"


def test_finder_covers_observed_false_positive_modes() -> None:
    # The finder false-fire cases are seeded from the REAL observed-FP taxonomy.
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    modes = {c.get("mode") for c in ds if c.get("expect") == "pass"}
    for required in (
        "domain-inappropriate-standard-import",
        "library-guaranteed-capability",
        "rebuild-of-designated-experiment-reference",
        "low-impact-nitpick",
    ):
        assert required in modes, f"finder eval missing false-fire mode {required!r}"


def test_finder_has_t12_discrimination_pair() -> None:
    # 7284 AC: T12 validated for DISCRIMINATION — a recall case (deployed-system change
    # → fire) AND a suppression case (library/CLI → not-applicable), not just suppression.
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    t12 = [c for c in ds if c.get("criterion") == "T12"]
    assert any(c.get("expect") == "finding" for c in t12), "T12 needs a recall (fire) case"
    assert any(c.get("expect") == "pass" for c in t12), "T12 needs a suppression case"


def test_verifier_has_discrimination_pairs() -> None:
    # acc1 AC: a labeled set of {true finding, planted FALSE finding}; Pass-2 must give
    # the false ones LOWER validity. Assert the pairs + both polarities exist.
    ds = E.load_eval_spec("plan-review-verifier").get("dataset", [])
    kinds = {c.get("kind") for c in ds}
    assert kinds == {"true", "false"}, "verifier eval needs both true and planted-false cases"
    pairs = {c.get("pair") for c in ds if c.get("pair")}
    assert pairs, "verifier eval needs linked true/false discrimination pairs"
    # The sycophancy (false-negative) axis is asserted too.
    names = {s["name"] for s in E.load_eval_spec("plan-review-verifier")["scorers"]}
    assert "discriminates_true_from_false" in names
    assert "no_sycophancy_on_real_defects" in names


def test_verifier_new_subanswers() -> None:
    # WS1 (epic cite-stone-sea) AC: the two DSO-adopted sub-answers appear in the Pass-2
    # verification contract. (Deeper kernel-math coverage — the na-default and old-sidecar
    # validity comparability — lives in tests/unit/test_review_kernel_rules.py.)
    from rebar.llm import review_kernel

    model = review_kernel.verification_model()
    binary = (
        model.model_fields["verifications"].annotation.__args__[0].model_fields["binary"].annotation
    )
    contract = set(binary.model_fields)
    for key in ("committed_work_relies_on_unbacked_claim", "respects_artifact_altitude"):
        assert key in review_kernel.GRADED_BINARY, f"{key} missing from GRADED_BINARY"
        assert key in contract, f"{key} missing from the verification contract"


def test_verifier_committed_claim_and_altitude_pairs() -> None:
    # WS1 (epic cite-stone-sea): the two DSO-adopted sub-answers each get a labeled
    # discrimination pair — a genuinely-unbacked committed claim / an undeclared-at-this-level
    # finding SURVIVES (high validity); a claim the plan backs / a wrong-altitude finding DROPS.
    ds = E.load_eval_spec("plan-review-verifier").get("dataset", [])
    for pair in ("committed-claim", "altitude"):
        cases = [c for c in ds if c.get("pair") == pair]
        kinds = {c.get("kind") for c in cases}
        assert kinds == {"true", "false"}, (
            f"{pair} pair needs both a recall and a planted-false case"
        )
        expects = {c.get("expect") for c in cases}
        assert "high_validity" in expects and "low_validity" in expects, (
            f"{pair} pair needs a surviving (high_validity) and a dropped (low_validity) case"
        )


def test_finder_has_scale_calibration_cases() -> None:
    # WS6 (epic cite-stone-sea): the G-9 scale anchors are finder-side (A1/T5a), proven as
    # fire/no-fire — evidenced high scale FIRES; scale invented by subject-interpolation does NOT.
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    by_id = {c.get("id"): c for c in ds}
    assert by_id.get("R-T5a-scale-evidenced", {}).get("expect") == "finding"
    fp = by_id.get("FP-T5a-inflated-scale", {})
    assert fp.get("expect") == "pass" and fp.get("mode") == "inflated-scale-interpolation"


def test_e4_scope_exclusion() -> None:
    # WS2 (epic cite-stone-sea): E4's scope-exclusion sub-check (G-4) discriminates — a false
    # CODEBASE exclusion FIRES; an external-fact exclusion ABSTAINS-with-coverage (no fire).
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    e4 = {c.get("id"): c for c in ds if c.get("criterion") == "E4"}
    assert e4.get("R-E4-scope-exclusion-codebase", {}).get("expect") == "finding"
    assert e4.get("FP-E4-scope-exclusion-external", {}).get("expect") == "pass"


def test_hedge_finder() -> None:
    # WS2: the hedge finder is a 1-TURN criterion (NOT exec:DET — ADR 0033) that fires on a
    # committed element resting on a hedged assumption.
    import json
    from pathlib import Path

    from rebar.llm.plan_review import registry

    routing = json.loads((Path(registry.__file__).parent / "criteria_routing.json").read_text())
    assert routing["hedge"]["exec"] == "1-TURN", "hedge must be exec:1-TURN, not exec:DET"
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    hedge = [c for c in ds if c.get("criterion") == "hedge"]
    assert any(c.get("expect") == "finding" for c in hedge), "hedge needs a recall (fire) case"


def test_hedge_dedup() -> None:
    # WS2: rubric-level dedup vs E6 — a hedge inside an AC proving-command clause is E6's
    # no_hedges territory; the hedge criterion marks itself not-applicable (no double-report).
    from pathlib import Path

    from rebar.llm.plan_review import passes

    prompt = (
        Path(passes.__file__).parent.parent / "reviewers" / "plan_review_hedge.md"
    ).read_text()
    assert "not-applicable" in prompt and "E6" in prompt, (
        "hedge prompt must carry the E6 dedup rule"
    )
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    dedup = [
        c for c in ds if c.get("criterion") == "hedge" and c.get("mode") == "ac-clause-e6-territory"
    ]
    assert dedup and dedup[0].get("expect") == "pass", (
        "need an AC-clause dedup (not-applicable) case"
    )


def _reviewer_prompt(name: str) -> str:
    from pathlib import Path

    from rebar.llm.plan_review import passes

    return (Path(passes.__file__).parent.parent / "reviewers" / name).read_text()


def test_g5_prohibition() -> None:
    # WS3 (epic cite-stone-sea): the prohibition-enumeration overlay (gap-report G-5, id T13).
    # Recall: a require-tests-before-merge plan yields UNCOVERED `gh pr merge` sites; FP: a plan
    # that merely describes existing enforcement does not fire. Enums defined in the prompt.
    prompt = _reviewer_prompt("plan_review_T13.md")
    for token in ("MIGRATED", "EXEMPTED", "UNCOVERED"):
        assert token in prompt, f"T13 prompt missing enum value {token}"
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    t13 = {c.get("id"): c for c in ds if c.get("criterion") == "T13"}
    assert t13.get("R-T13-prohibition-uncovered", {}).get("expect") == "finding"
    assert t13.get("FP-T13-describes-enforcement", {}).get("expect") == "pass"


def test_g10_citrigger() -> None:
    # WS3: the CI-trigger/release-infra overlay (gap-report G-10, id T14). Recall: a new ref
    # pattern yields an EXCLUDED workflow; plus a fail-open abstain on unknown CI. Enums in prompt.
    prompt = _reviewer_prompt("plan_review_T14.md")
    for token in ("INCLUDED", "EXCLUDED", "NO_FILTER"):
        assert token in prompt, f"T14 prompt missing enum value {token}"
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    t14 = {c.get("id"): c for c in ds if c.get("criterion") == "T14"}
    assert t14.get("R-T14-excluded-workflow", {}).get("expect") == "finding"
    failopen = t14.get("FP-T14-unknown-ci-failopen", {})
    assert failopen.get("expect") == "pass" and failopen.get("mode") == "unknown-ci-fail-open"


def _finder_cases_for(criterion: str) -> dict:
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    return {c.get("id"): c for c in ds if c.get("criterion") == criterion}


def test_removal_rationale_exempt() -> None:
    # WS11 (epic cite-stone-sea): the gate EXEMPTS dead-code / behavior-preserving refactor.
    c = _finder_cases_for("removal-rationale").get("FP-removal-rationale-dead-code", {})
    assert c.get("expect") == "pass" and c.get("mode") == "dead-code-exempt"


def test_removal_rationale_error_path() -> None:
    # WS11: the gate FIRES on an internals change that alters error/failure handling.
    c = _finder_cases_for("removal-rationale").get("R-removal-rationale-error-path", {})
    assert c.get("expect") == "finding" and c.get("mode") == "error-handling-change"


def test_removal_rationale_intent_marker_and_fabricated() -> None:
    # WS11: FIRES on removal of an intent-marked artifact (no happy-path delta); a fabricated /
    # ungrounded justification does not satisfy the grounded-scenario bar — the case pins the
    # expected sub-answer removal_scenario_grounded ∈ {no, insufficient} (low validity).
    cases = _finder_cases_for("removal-rationale")
    assert cases.get("R-removal-rationale-intent-marker", {}).get("expect") == "finding"
    fabricated = cases.get("R-removal-rationale-fabricated", {})
    assert fabricated.get("mode") == "fabricated-scenario"
    assert fabricated.get("expect") == "finding"
    grounded = fabricated.get("checklist_expect", {}).get("removal_scenario_grounded")
    assert grounded in ("no", "insufficient"), (
        "an invented scenario must score low on removal_scenario_grounded"
    )


def test_removal_rationale_advisory_and_coaching() -> None:
    # WS11: advisory posture (does not block a claim) + coaching reuses move 6, and the E5
    # test-removal overlap is grouped by the coaching pass (a recall case where both fire).
    import json
    from pathlib import Path

    from rebar.llm.plan_review import passes, registry

    routing = json.loads((Path(registry.__file__).parent / "criteria_routing.json").read_text())
    assert routing["removal-rationale"]["default_posture"] == "advisory"
    prompt = (
        Path(passes.__file__).parent.parent / "reviewers" / "plan_review_removal_rationale.md"
    ).read_text()
    assert "move 6" in prompt, "removal-rationale must frame its ask as coach move 6"
    assert _finder_cases_for("removal-rationale").get("R-removal-rationale-e5-overlap")


def test_verifier_has_blast_radius_ratchet_pair() -> None:
    # WS6 FP-3(b): the one-way blast_radius ratchet is a Pass-2 (verifier) behavior, so it is
    # exercised in the verifier eval — a real system-wide defect keeps impact; a trivial finding
    # is NOT inflated to blocking by a grand subject. (discriminates_impact_levels scores it.)
    ds = E.load_eval_spec("plan-review-verifier").get("dataset", [])
    cases = [c for c in ds if c.get("pair") == "blast-radius-ratchet"]
    assert {c.get("kind") for c in cases} == {"true", "false"}
    assert {c.get("expect") for c in cases} == {"high_impact", "low_impact"}


def test_finder_has_proxy_validation_cutover_cases() -> None:
    # super-plant-liver AC: the B5-shaped scenario (a cutover that DEFAULTS to a new path
    # whose AC is satisfiable by offline/mocked tests that never exercise the live path)
    # is encoded as a recall case for E5 (runs on stories) and E6 (the ac-text-quality
    # proving-command angle), each paired with a good case so the new flag discriminates
    # rather than firing on every cutover. (The fire/no-fire assertion itself runs in the
    # gated live-model eval CI; this offline test pins the cases' presence.)
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    recall = {(c.get("criterion"), c.get("id")) for c in ds if c.get("expect") == "finding"}
    assert ("E5", "R-E5-proxy-validation-cutover") in recall
    assert ("E6", "R-E6-cutover-no-live-ac") in recall
    # The anti-over-fire pair: a cutover that DOES exercise the defaulted path live → PASS.
    assert any(
        c.get("id") == "FP8-E5-cutover-has-live-ac" and c.get("expect") == "pass" for c in ds
    )


def test_isf_has_recall_and_justified_descope_cases() -> None:
    # 681b AC: ISF recall (silent drop) + no false-fire on a justified descope.
    ds = E.load_eval_spec("plan-review-isf-finder").get("dataset", [])
    assert any(c.get("expect") == "finding" for c in ds)
    assert any(c.get("mode") == "justified-descope" for c in ds)


@pytest.mark.parametrize("prompt_id", PLAN_REVIEW_SPECS)
def test_gold_subset_present_for_judge_alignment(prompt_id: str) -> None:
    # The human-adjudicated gold subset the llm-judge is kappa-aligned to.
    assert E.load_eval_spec(prompt_id).get("gold_set"), f"{prompt_id} needs a gold_set"


def test_container_covers_both_g3_and_g4() -> None:
    # da34 AC: the container spec covers G3 (child coverage) AND G4 (child consistency),
    # each with a recall (fire) case AND a false-fire (suppression) case — discrimination,
    # not blanket firing. The ANTI-FP modes from the G3/G4 rubrics are encoded explicitly.
    ds = E.load_eval_spec("plan-review-container").get("dataset", [])
    for crit in ("G3", "G4"):
        cases = [c for c in ds if c.get("criterion") == crit]
        assert any(c.get("expect") == "finding" for c in cases), f"{crit} needs a recall case"
        assert any(c.get("expect") == "pass" for c in cases), f"{crit} needs a suppression case"
    modes = {c.get("mode") for c in ds if c.get("expect") == "pass"}
    # G3 ANTI-FP (covered-by-named-consumer) + G4 ANTI-FP (benign reading) are present.
    assert "covered-by-named-consumer" in modes
    assert "benign-reading" in modes


def test_container_documents_numeric_tolerance() -> None:
    # da34 AC: an EXPLICIT, documented numeric tolerance for the candidate-vs-baseline
    # diff. It must be present in the spec AND agree with the parity.py constants (the
    # canonical source the S4/S5 gate enforces) so the two cannot drift.
    from rebar.llm import parity

    tol = E.load_eval_spec("plan-review-container").get("tolerance", {})
    assert tol.get("finding_recall_margin") == parity.NONINFERIORITY_MARGIN
    assert tol.get("false_accept_margin") == parity.NONINFERIORITY_MARGIN
    assert tol.get("attribution_accuracy_floor") == parity.ATTRIBUTION_ACCURACY_FLOOR
    assert tol.get("min_gold") == parity.CONTAINER_MIN_GOLD


def test_container_corpus_meets_its_own_min_gold_floor() -> None:
    # The container spec gates on min_gold=CONTAINER_MIN_GOLD; its SHIPPED labelled corpus
    # must satisfy that floor, else container_fidelity_report would FAIL on its own dataset
    # ("gold set too small to certify"). Guards the corpus from silently dropping below the
    # floor it gates on — keep the documented floor honest, not the corpus shrinking under it.
    from rebar.llm import parity

    gold = E.load_eval_spec("plan-review-container").get("gold_set", [])
    assert len(gold) >= parity.CONTAINER_MIN_GOLD, (
        f"container gold_set has {len(gold)} items < CONTAINER_MIN_GOLD "
        f"({parity.CONTAINER_MIN_GOLD}) — the gate would fail on its own corpus"
    )
    # Balanced + discriminating: both finding (recall) and pass (false-fire) labels present.
    assert {"finding", "pass"} <= {g.get("label") for g in gold}
