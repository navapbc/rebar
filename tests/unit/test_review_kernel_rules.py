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
    # the 7 graded sub-questions are each their own field, plus the conditional veto
    assert review_kernel.GRADED_BINARY and set(review_kernel.GRADED_BINARY) <= fields
    assert "cited_reference_accurate" in fields
    assert len(fields) == len(review_kernel.GRADED_BINARY) + 1


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
