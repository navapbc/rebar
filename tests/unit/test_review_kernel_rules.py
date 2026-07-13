"""WS4: the verifier soft-rules are OBEYED — a BEHAVIORAL eval, NOT a prompt-text lint.

The four soft rules (recorded in ``review_kernel.VERIFIER_RULES_SCAFFOLD``) are enforced by
OBSERVABLE BEHAVIOR on the gate path, not by grepping prompt strings (a brittle, gameable
anti-pattern — see docs/review-kernel.md). These deterministic assertions run in CI; a small
GATED live counterpart lives in ``tests/external/test_review_kernel_rules_live.py`` (off the
blocking path). Each rule maps to a structural/behavioral property the kernel guarantees:

* independence — the verifier INSTRUCTIONS never carry the finding's own decision/severity;
* atomicity — each binary sub-question is an INDEPENDENT contract field;
* allow-insufficient — ``insufficient`` is graded honestly (0.5), not silently dropped;
* verdict-with-citation-not-fix — the ``verification`` contract has NO fix/remediation field.
"""

from __future__ import annotations

import importlib

import pytest

from rebar.llm import review_kernel

pytestmark = pytest.mark.unit

kverify = importlib.import_module("rebar.llm.review_kernel.verify")


def _binary_fields() -> set[str]:
    model = review_kernel.verification_model()
    return set(
        model.model_fields["verifications"]
        .annotation.__args__[0]
        .model_fields["binary"]
        .annotation.model_fields
    )


def test_scaffold_records_all_four_rules() -> None:
    keys = {name for name, _text in kverify.VERIFIER_RULES}
    assert keys == {
        "independence",
        "atomicity",
        "allow-insufficient",
        "verdict-with-citation-not-fix",
    }
    # the scaffold a gate's preamble embeds is a non-empty, single-source snippet
    assert all(name in review_kernel.VERIFIER_RULES_SCAFFOLD for name in keys)


def test_independence_instructions_never_leak_the_findings_conclusion() -> None:
    """A verifier must treat the finding as an unproven claim: the INSTRUCTIONS it receives carry
    claim/criteria/evidence/impact but NOT the finding's own decision/severity/validity/priority
    (even when those were stamped on the finding by a prior pass)."""
    finding = {
        "finding": "the retry policy is unbounded",
        "criteria": ["E1"],
        "evidence": ["plan says 'retry forever'"],
        "impact": "thundering-herd on the dependency",
        # a hostile fixture: a prior pass's conclusion is present on the dict…
        "decision": "block",
        "severity": "critical",
        "validity": 1.0,
        "priority": 1.0,
    }
    instructions = kverify.verify_instructions([(0, finding)])
    assert "the retry policy is unbounded" in instructions  # the claim IS shown
    # …but the conclusion is NOT leaked to the independent verifier
    for leak in ("block", "critical", "validity", "priority"):
        assert leak not in instructions, f"independence violated: '{leak}' leaked to the verifier"


def test_atomicity_each_binary_subquestion_is_an_independent_field() -> None:
    fields = _binary_fields()
    # each graded sub-question is its own field, plus the THREE conditional veto binaries
    # (cited_reference_accurate + the a8e5 absence-claim pair) — count stays
    # len(GRADED_BINARY) + 3 as the graded tuple grows; the vetoes are NOT graded.
    assert review_kernel.GRADED_BINARY and set(review_kernel.GRADED_BINARY) <= fields
    assert "cited_reference_accurate" in fields
    _absence = {"claims_absence", "absence_confirmed_in_context"}
    assert _absence <= fields
    assert _absence.isdisjoint(review_kernel.GRADED_BINARY)
    assert len(fields) == len(review_kernel.GRADED_BINARY) + 3


def test_verifier_new_subanswers_are_in_the_verification_contract() -> None:
    """WS1 (epic cite-stone-sea): the two DSO-adopted sub-answers appear as graded binary
    fields in the verification contract AND in decide.GRADED_BINARY (so they participate in
    validity through the uniform loop, not a criterion-specific branch)."""
    fields = _binary_fields()
    for key in ("committed_work_relies_on_unbacked_claim", "respects_artifact_altitude"):
        assert key in review_kernel.GRADED_BINARY, f"{key} missing from GRADED_BINARY"
        assert key in fields, f"{key} missing from the verification contract"


def test_new_subanswers_default_to_na_and_are_excluded_from_validity() -> None:
    """WS1: the two new sub-answers default to `na` in the Binary model — so a verifier that
    does not engage them ABSTAINS (excluded from the validity mean) rather than dragging it,
    while the pre-existing sub-answers keep their `insufficient` default."""
    binary_model = (
        review_kernel.verification_model()
        .model_fields["verifications"]
        .annotation.__args__[0]
        .model_fields["binary"]
        .annotation
    )
    defaults = {name: f.default for name, f in binary_model.model_fields.items()}
    assert defaults["committed_work_relies_on_unbacked_claim"] == "na"
    assert defaults["respects_artifact_altitude"] == "na"
    assert defaults["is_verifiable"] == "insufficient"  # unchanged sub-answers stay insufficient
    # `na` is excluded from the mean: an otherwise-perfect finding that abstains on the two
    # new keys grades exactly the same as one that never carried them.
    perfect = {q: "yes" for q in review_kernel.GRADED_BINARY}
    with_na = {
        **perfect,
        "committed_work_relies_on_unbacked_claim": "na",
        "respects_artifact_altitude": "na",
    }
    assert review_kernel.validity(with_na) == 1.0


def test_old_sidecar_findings_stay_validity_comparable() -> None:
    """WS1 old-sidecar comparability: a pre-change finding dict (which lacks the two new keys
    entirely) grades to exactly the validity it did before they were introduced — the absent
    keys are non-answerable, identical to `na`, and never enter the mean."""
    pre_change = {
        "is_verifiable": "yes",
        "evidence_entails_finding": "yes",
        "path_reachable": "no",
        "impact_follows_necessarily": "insufficient",
        "no_viable_alternative_explanation": "yes",
        "no_existing_mitigation": "yes",
        "severity_claim_justified": "no",
    }
    # 7 answered keys: yes,yes,no,insufficient,yes,yes,no -> (1+1+0+0.5+1+1+0)/7, rounded 4dp.
    expected = round((1 + 1 + 0 + 0.5 + 1 + 1 + 0) / 7, 4)
    assert review_kernel.validity(pre_change) == expected
    # Adding the new keys as na (or omitting them) does not change the score.
    assert (
        review_kernel.validity(
            {
                **pre_change,
                "committed_work_relies_on_unbacked_claim": "na",
                "respects_artifact_altitude": "na",
            }
        )
        == expected
    )


def test_allow_insufficient_is_graded_honestly_not_dropped() -> None:
    """'insufficient' is an honest answer: it grades to 0.5 (not 0), and an all-insufficient
    finding surfaces as ADVISORY (validity 0.5 is NOT below the 0.5 drop floor) — never silently
    dropped for being uncertain."""
    assert review_kernel.validity({q: "insufficient" for q in review_kernel.GRADED_BINARY}) == 0.5
    verif = {
        "binary": {q: "insufficient" for q in review_kernel.GRADED_BINARY},
        "severity_attributes": {"prod_impact": "medium"},
    }
    assert review_kernel.pass3_decide(verif)["decision"] == "advisory"


def test_verdict_with_citation_not_fix_contract_has_no_fix_field() -> None:
    """The verifier renders a verdict-with-citation, never a fix: the verification contract
    carries severity_attributes + the binary sub-answers + the index (+ a REASON-FIRST `analysis`
    scratchpad) — but NO fix/remediation field for the model to author a fix into."""
    model = review_kernel.verification_model()
    verification = model.model_fields["verifications"].annotation.__args__[0]
    fields = set(verification.model_fields)
    # The judgement fields (index + severity + binary) plus the reasoning scratchpad — nothing else.
    assert fields == {"index", "analysis", "severity_attributes", "binary"}
    for forbidden in ("fix", "suggested_fix", "remediation", "patch"):
        assert forbidden not in fields


def test_gate_path_honors_insufficient_end_to_end_offline() -> None:
    """A rules-conformant verifier (answering 'insufficient' where the evidence does not decide)
    flows through verify_findings → pass3 and SURFACES the finding as advisory, offline."""
    findings = [{"finding": "f0", "criteria": ["E1"], "evidence": [], "impact": ""}]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        # an honest verifier: uncertain ⇒ insufficient (not a fabricated yes/no)
        return [
            {
                "index": 0,
                "severity_attributes": {"prod_impact": "low"},
                "binary": {q: "insufficient" for q in review_kernel.GRADED_BINARY},
            }
        ]

    result = kverify.verify_findings(
        findings,
        context="the plan",
        run_chunk=run_chunk,
        window_tokens=1_000_000,
        est_tokens=lambda s: len(s) // 4,
    )
    decided = review_kernel.pass3_over_findings(
        findings, result["verifications"], threshold_for=lambda crit: (0.95, False)
    )
    assert decided[0]["decision"] == "advisory"  # honest 'insufficient' surfaces, not dropped
