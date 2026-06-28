"""The shared four-pass review KERNEL (epic ``vivid-gang-day``).

WS1 — the Pass-3 deterministic decision core (:mod:`rebar.llm.review_kernel.decide`):

* ground-truth behavioral assertions on the math (validity / impact / priority / the
  decision labels) BY CONSTRUCTION — not a snapshot of prior output, so the test cannot
  lock in a pre-existing bug;
* the per-criterion ``block_threshold`` is a PARAMETER: two consumers with different
  thresholds route through the SAME kernel and produce independently-correct partitions
  (the divergence-danger this extraction removes);
* the plan-review re-exports are the SAME objects as the kernel's (no second copy of the
  decision math remains, AC #3).
"""

from __future__ import annotations

import pytest

from rebar.llm import review_kernel
from rebar.llm.review_kernel import decide as kdecide

pytestmark = pytest.mark.unit


def _verif(binary=None, attrs=None) -> dict:
    base_b = {q: "yes" for q in review_kernel.GRADED_BINARY}
    base_b["cited_reference_accurate"] = "na"
    base_a = {
        "prod_impact": "high",
        "debt_impact": "high",
        "blast_radius": "system",
        "likelihood": "high",
        "reversibility": "hard",
    }
    return {
        "binary": {**base_b, **(binary or {})},
        "severity_attributes": {**base_a, **(attrs or {})},
    }


# ── ground-truth math (by construction) ───────────────────────────────────────
def test_validity_is_the_graded_fraction() -> None:
    assert review_kernel.validity({q: "yes" for q in review_kernel.GRADED_BINARY}) == 1.0
    assert review_kernel.validity({q: "no" for q in review_kernel.GRADED_BINARY}) == 0.0
    assert review_kernel.validity({q: "insufficient" for q in review_kernel.GRADED_BINARY}) == 0.5
    # 'na' answers are excluded from the graded denominator; empty ⇒ 0.0.
    assert review_kernel.validity({}) == 0.0


def test_impact_is_mean_of_ordinal_attributes() -> None:
    # all-max ⇒ 1.0; all-floor ⇒ the ordinal floors averaged (none/local/low/easy).
    assert review_kernel.impact(_verif()["severity_attributes"]) == 1.0
    floor = {
        "prod_impact": "none",
        "debt_impact": "none",
        "blast_radius": "local",
        "likelihood": "low",
        "reversibility": "easy",
    }
    assert review_kernel.impact(floor) == round((0.0 + 0.33 + 0.33 + 0.33) / 4.0, 4)
    # prod/debt take the MAX of the two, not the sum.
    assert review_kernel.impact(
        {"prod_impact": "high", "debt_impact": "none"}
    ) == review_kernel.impact({"prod_impact": "none", "debt_impact": "high"})


def test_severity_label_buckets() -> None:
    assert review_kernel.severity_label(0.8) == "critical"
    assert review_kernel.severity_label(0.6) == "major"
    assert review_kernel.severity_label(0.3) == "minor"
    assert review_kernel.severity_label(0.1) == "none"


def test_decision_labels_by_construction() -> None:
    # no verification ⇒ indeterminate
    assert review_kernel.pass3_decide(None)["decision"] == "indeterminate"
    # all-yes, max severity, blocking opted in + over threshold ⇒ block
    assert review_kernel.pass3_decide(_verif(), blocking_enabled=True)["decision"] == "block"
    # same finding, blocking NOT opted in ⇒ advisory (the v1 default posture)
    assert review_kernel.pass3_decide(_verif(), blocking_enabled=False)["decision"] == "advisory"
    # validity < 0.5 ⇒ dropped (low validity)
    low = _verif(binary={q: "no" for q in list(review_kernel.GRADED_BINARY)[:5]})
    assert review_kernel.pass3_decide(low)["decision"] == "dropped"
    # the cited-reference veto drops even a high-validity finding
    vetoed = _verif(binary={"cited_reference_accurate": "no"})
    assert review_kernel.pass3_decide(vetoed)["decision"] == "dropped"


# ── the threshold is a PARAMETER: two consumers, one kernel, independent partitions ──
def test_parameterized_threshold_two_consumers_one_kernel() -> None:
    """A mid-priority finding (validity 1.0 × impact 0.5 = 0.5): a STRICT gate
    (threshold 0.95, blocking on) leaves it ADVISORY; a LENIENT gate (threshold 0.4,
    blocking on) BLOCKS it. Same kernel math, different parameterized posture — the
    extraction's whole point (no forked decision core)."""
    mid = _verif(
        attrs={
            "prod_impact": "low",
            "debt_impact": "low",
            "blast_radius": "module",
            "likelihood": "medium",
            "reversibility": "moderate",
        }
    )
    priority = review_kernel.pass3_decide(mid, blocking_enabled=True, block_threshold=0.0)[
        "priority"
    ]
    assert 0.0 < priority < 0.95
    strict = review_kernel.pass3_decide(mid, block_threshold=0.95, blocking_enabled=True)
    lenient = review_kernel.pass3_decide(mid, block_threshold=priority, blocking_enabled=True)
    assert strict["decision"] == "advisory"
    assert lenient["decision"] == "block"


def test_pass3_over_findings_uses_the_threshold_resolver() -> None:
    """``pass3_over_findings`` resolves the per-finding posture via the consumer-supplied
    callable, keyed on each finding's criteria — proving the lookup is parameterized, not
    hardcoded."""
    findings = [{"finding": "a", "criteria": ["STRICT"]}, {"finding": "b", "criteria": ["LENIENT"]}]
    verifs = {0: _verif(), 1: _verif()}  # both max priority (1.0)

    def threshold_for(criteria):
        # STRICT never blocks (threshold above max); LENIENT blocks (opted in, low threshold).
        if "LENIENT" in criteria:
            return 0.5, True
        return 1.5, True

    decided = review_kernel.pass3_over_findings(findings, verifs, threshold_for=threshold_for)
    assert [d["decision"] for d in decided] == ["advisory", "block"]
    # each decided finding carries its verification + the LLM tier marker
    assert all(d["tier"] == "LLM" and d["verification"] is not None for d in decided)


# ── no second copy: the plan-review re-exports ARE the kernel objects (AC #3) ──
def test_plan_review_reexports_are_the_kernel_objects() -> None:
    from rebar.llm.plan_review import passes

    assert passes.pass3_decide is kdecide.pass3_decide
    assert passes.validity is kdecide.validity
    assert passes.impact is kdecide.impact
    assert passes.severity_label is kdecide.severity_label
    assert passes.GRADED_BINARY is kdecide.GRADED_BINARY
    assert passes.DEFAULT_BLOCK_THRESHOLD == kdecide.DEFAULT_BLOCK_THRESHOLD
