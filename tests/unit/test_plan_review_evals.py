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


def test_isf_has_recall_and_justified_descope_cases() -> None:
    # 681b AC: ISF recall (silent drop) + no false-fire on a justified descope.
    ds = E.load_eval_spec("plan-review-isf-finder").get("dataset", [])
    assert any(c.get("expect") == "finding" for c in ds)
    assert any(c.get("mode") == "justified-descope" for c in ds)


@pytest.mark.parametrize("prompt_id", PLAN_REVIEW_SPECS)
def test_gold_subset_present_for_judge_alignment(prompt_id: str) -> None:
    # The human-adjudicated gold subset the llm-judge is kappa-aligned to.
    assert E.load_eval_spec(prompt_id).get("gold_set"), f"{prompt_id} needs a gold_set"
