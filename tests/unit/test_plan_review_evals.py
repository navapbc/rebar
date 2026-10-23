"""Offline plan-review eval guards. Each shipped spec parses and includes recall, false-accept, and
false-fire cases, including Pass-2 discrimination and justified scope exclusions.
Provider-backed model evaluation runs separately with the agents extra.
Its seed labels await human adjudication."""

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
    # The Pass-2 contract includes both adopted sub-answers. Kernel math is tested separately.
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
    # Pass-2 retains unbacked claims and undeclared-at-this-level findings. It drops backed
    # claims and wrong-altitude findings.
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
    # Finder cases fire on evidenced scale and reject scale inferred only from the subject.
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    by_id = {c.get("id"): c for c in ds}
    assert by_id.get("R-T5a-scale-evidenced", {}).get("expect") == "finding"
    fp = by_id.get("FP-T5a-inflated-scale", {})
    assert fp.get("expect") == "pass" and fp.get("mode") == "inflated-scale-interpolation"


def test_e4_scope_exclusion() -> None:
    # E4 fires on false codebase exclusions and abstains with coverage on external facts.
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    e4 = {c.get("id"): c for c in ds if c.get("criterion") == "E4"}
    assert e4.get("R-E4-scope-exclusion-codebase", {}).get("expect") == "finding"
    assert e4.get("FP-E4-scope-exclusion-external", {}).get("expect") == "pass"


def test_hedge_finder() -> None:
    # The one-turn hedge finder flags committed claims resting on hedged assumptions.
    import json
    from pathlib import Path

    from rebar.llm.plan_review import registry

    routing = json.loads((Path(registry.__file__).parent / "criteria_routing.json").read_text())
    assert routing["hedge"]["exec"] == "1-TURN", "hedge must be exec:1-TURN, not exec:DET"
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    hedge = [c for c in ds if c.get("criterion") == "hedge"]
    assert any(c.get("expect") == "finding" for c in hedge), "hedge needs a recall (fire) case"


def test_hedge_dedup() -> None:
    # E6 owns hedges in acceptance-criteria proof commands. The hedge finder abstains there.
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
    # G5 finds prohibited merge commands in test-before-merge plans but ignores descriptions of
    # existing enforcement.
    prompt = _reviewer_prompt("plan_review_T13.md")
    for token in ("MIGRATED", "EXEMPTED", "UNCOVERED"):
        assert token in prompt, f"T13 prompt missing enum value {token}"
    ds = E.load_eval_spec("plan-review-finder").get("dataset", [])
    t13 = {c.get("id"): c for c in ds if c.get("criterion") == "T13"}
    assert t13.get("R-T13-prohibition-uncovered", {}).get("expect") == "finding"
    assert t13.get("FP-T13-describes-enforcement", {}).get("expect") == "pass"


def test_g10_citrigger() -> None:
    # G10 detects new refs excluded from CI and abstains when the CI provider is unknown.
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
    # Dead-code removal and behavior-preserving refactors are exempt.
    c = _finder_cases_for("removal-rationale").get("FP-removal-rationale-dead-code", {})
    assert c.get("expect") == "pass" and c.get("mode") == "dead-code-exempt"


def test_removal_rationale_error_path() -> None:
    # Changed error handling requires removal rationale.
    c = _finder_cases_for("removal-rationale").get("R-removal-rationale-error-path", {})
    assert c.get("expect") == "finding" and c.get("mode") == "error-handling-change"


def test_removal_rationale_intent_marker_and_fabricated() -> None:
    # Intent-marked removals require a grounded scenario. Fabricated rationale does not qualify.
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
    # Removal rationale is advisory. Coaching groups its overlap with E5 test removal.
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
    # Pass-2 retains impact for a system-wide defect without inflating a trivial finding from
    # the subject's scope.
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


def test_every_container_case_carries_a_children_payload() -> None:
    # Ticket 3e8b: a CONTAINER criterion runs `pass1_container` over a
    # (parent, children, sibling-roster) decomposition, so `_run_criterion_case` RAISES on a
    # case that carries only the parent plan as `input` — "a fixture error, not a silent
    # pass". Every container case must therefore ship a non-empty `children` list of
    # {ticket_id, title, description} entries (the shape decomp-shape already uses). Without
    # this gate a new payload-less case silently drops out of `provider_parity.eligible_cases`
    # instead of failing — which is exactly how the original 12 went unnoticed.
    ds = E.load_eval_spec("plan-review-container").get("dataset", [])
    assert ds, "container spec has no dataset"
    for case in ds:
        children = case.get("children")
        assert isinstance(children, list) and children, (
            f"container case {case.get('id')!r} has no `children` payload — "
            f"`run_case` can only raise on it, so it is not corpus"
        )
        for child in children:
            missing = {"ticket_id", "title", "description"} - set(child or {})
            assert not missing, (
                f"container case {case.get('id')!r} has a child missing {sorted(missing)}"
            )


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
