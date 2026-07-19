"""R5 (story empty-microbial-antlion) + E5 (pisciform-spineless-wobbegong).

Pins the invariant that makes R5 safe to land: adding the na-default sub-answer
``asserted_capability_confirmed`` to the shared kernel ``GRADED_BINARY`` is BYTE-IDENTICAL for
every finding where it is ``na``/absent (which is every finding outside the G6/E4/T3 cohort),
while STILL contributing to validity when the verifier answers it (so it is not a dead field).
"""

from __future__ import annotations

import pytest

from rebar.llm.review_kernel import decide, verify_models

pytestmark = pytest.mark.unit

R5_FIELD = "asserted_capability_confirmed"
PRE_GRADED = tuple(q for q in decide.GRADED_BINARY if q != R5_FIELD)


def test_field_registered_and_na_default() -> None:
    """The sub-answer is a graded binary AND defaults to ``na`` (abstains), in the shared vocab
    and in every gate's built Binary model."""
    assert R5_FIELD in decide.GRADED_BINARY
    assert R5_FIELD in verify_models._BINARY_NA_DEFAULT
    for model_fn in (
        verify_models.verification_model,
        verify_models.plan_review_verification_model,
        verify_models.code_review_verification_model,
    ):
        binary_cls = (
            model_fn()
            .model_fields["verifications"]
            .annotation.__args__[0]
            .model_fields["binary"]
            .annotation
        )
        field = binary_cls.model_fields[R5_FIELD]
        assert field.default == "na"


@pytest.mark.parametrize(
    "binary",
    [
        {},
        {q: "yes" for q in PRE_GRADED},
        {q: "no" for q in PRE_GRADED},
        {q: "insufficient" for q in PRE_GRADED},
        {
            "is_verifiable": "yes",
            "evidence_entails_finding": "no",
            "path_reachable": "insufficient",
        },
    ],
)
def test_na_or_absent_is_byte_identical(binary: dict[str, str]) -> None:
    """For any binary, ``validity`` and the full ``pass3_decide`` dict are identical whether the
    R5 field is ABSENT (a pre-R5 sidecar) or present as ``na`` (a post-R5 non-cohort finding)."""
    v_absent = decide.validity(binary)
    v_na = decide.validity({**binary, R5_FIELD: "na"})
    assert v_absent == v_na

    attrs = {"prod_impact": "high", "blast_radius": "system", "likelihood": "high"}
    pre = decide.pass3_decide(
        {"binary": binary, "severity_attributes": attrs}, blocking_enabled=True
    )
    post = decide.pass3_decide(
        {"binary": {**binary, R5_FIELD: "na"}, "severity_attributes": attrs}, blocking_enabled=True
    )
    assert pre == post


def test_cohort_answer_contributes_to_validity() -> None:
    """The field is NOT dead: answered ``no`` (the capability IS delivered — the non-delivery
    finding is refuted) it LOWERS validity like any graded sub-answer; answered ``yes`` (the gap
    is confirmed — the finding holds) it keeps validity up."""
    base = {"is_verifiable": "yes", "evidence_entails_finding": "yes"}
    assert decide.validity({**base, R5_FIELD: "no"}) < decide.validity(base)
    assert decide.validity({**base, R5_FIELD: "yes"}) == decide.validity(base) == 1.0


def test_pre_post_vocab_lengths() -> None:
    """R5 grows the graded set by exactly one (guards against an accidental second addition)."""
    assert len(decide.GRADED_BINARY) == len(PRE_GRADED) + 1
