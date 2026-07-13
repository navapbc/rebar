"""HELD-OUT edge tests for a8e5 Component 1 (absence-claim veto). Merge into
tests/unit/test_review_kernel.py after the implementer has only seen the happy path."""

import pytest

from rebar.llm import review_kernel

pytestmark = pytest.mark.unit


def _verif(binary=None, attrs=None):
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


def test_true_absence_survives_the_veto() -> None:
    # A GENUINE absence (verifier CONFIRMED absent: absence_confirmed_in_context == "yes")
    # is NOT vetoed — it blocks/advises on its own merits.
    v = _verif(binary={"claims_absence": "yes", "absence_confirmed_in_context": "yes"})
    d = review_kernel.pass3_decide(v, blocking_enabled=True)
    assert d["decision"] == "block"
    assert not str(d["reason"]).startswith("veto:absence")


def test_insufficient_context_does_not_veto() -> None:
    # Only a DEFINITE refutation ("no") vetoes; "insufficient" must not drop the finding.
    v = _verif(binary={"claims_absence": "yes", "absence_confirmed_in_context": "insufficient"})
    assert review_kernel.pass3_decide(v, blocking_enabled=True)["decision"] == "block"


def test_non_absence_finding_unaffected_even_if_context_no() -> None:
    # claims_absence defaults to "na" — a non-absence finding is never vetoed regardless of the
    # absence_confirmed_in_context value (guards a spurious veto on unrelated findings).
    v = _verif(binary={"claims_absence": "na", "absence_confirmed_in_context": "no"})
    assert review_kernel.pass3_decide(v, blocking_enabled=True)["decision"] == "block"
    v2 = _verif(binary={"absence_confirmed_in_context": "no"})  # claims_absence absent → default na
    assert review_kernel.pass3_decide(v2, blocking_enabled=True)["decision"] == "block"


def test_absence_binaries_absent_is_byte_compatible() -> None:
    # An older/absent verifier that emits neither absence binary (both default "na") behaves
    # exactly as before — no veto (code-review back-compat).
    v = _verif()
    assert review_kernel.pass3_decide(v, blocking_enabled=True)["decision"] == "block"


def test_absence_binaries_do_not_affect_validity() -> None:
    # The absence binaries are conditional vetoes, NOT graded — they never move validity.
    v_plain = _verif()
    v_abs = _verif(binary={"claims_absence": "yes", "absence_confirmed_in_context": "yes"})
    assert review_kernel.validity(v_plain["binary"]) == review_kernel.validity(v_abs["binary"])
    assert "claims_absence" not in review_kernel.GRADED_BINARY
    assert "absence_confirmed_in_context" not in review_kernel.GRADED_BINARY


def test_absence_veto_defaults_are_na_in_the_model() -> None:
    from rebar.llm.review_kernel import verify as kverify

    Binary = (
        kverify.verification_model()
        .model_fields["verifications"]
        .annotation.__args__[0]
        .model_fields["binary"]
        .annotation
    )
    inst = Binary()
    assert inst.claims_absence == "na"
    assert inst.absence_confirmed_in_context == "na"


def test_f1fa_1144_false_absence_regression_fixture() -> None:
    """Regression fixture replaying the f1fa 11:44 verdict (norm ne7642adbf79 + the G3 finding
    n0eb3f2e9da9): the blocking finding asserted the plan 'never tasks anyone with capturing the
    pre-cutover snapshot' — a FALSE absence (the plan named the actor, the artifact
    infra/gerrit/access-snapshot-pre-autolander.json, and the timing). Pass-3 must DROP it via the
    absence veto (claims_absence=yes AND absence_confirmed_in_context=no), while a genuinely-true
    absence in the SAME shape survives."""
    # norm ne7642adbf79 — the false-absence blocking finding the verifier confirmed was refuted
    false_absence = _verif(
        binary={"claims_absence": "yes", "absence_confirmed_in_context": "no"},
    )
    d = review_kernel.pass3_decide(false_absence, blocking_enabled=True)
    assert d["decision"] == "dropped" and d["reason"] == "veto:absence-refuted"
    # a TRUE absence (the verifier searched the plan and confirmed the item is genuinely absent)
    true_absence = _verif(
        binary={"claims_absence": "yes", "absence_confirmed_in_context": "yes"},
    )
    assert review_kernel.pass3_decide(true_absence, blocking_enabled=True)["decision"] == "block"
