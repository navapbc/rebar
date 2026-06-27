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

from rebar.llm import eval as E

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
