"""Per-finding ``remediation`` on the completion verdict (ticket 7226).

An operator-attested criterion judged NOT MET should carry a concrete next-move on the
FINDING itself (record proof: reference id, observed outcome, when) — distinct from the
generic top-level ``remediation`` set by ``reconcile_verdict``. These are deterministic
contract checks: the field exists on ``VerdictFinding`` and round-trips through a
serialized ``completion_verdict``.
"""

from __future__ import annotations


def test_verdict_finding_carries_optional_remediation() -> None:
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    Finding = Model.model_fields["findings"].annotation.__args__[0]

    # Present when supplied.
    f = Finding(
        criterion="[operator-attested] deploy confirmed in prod",
        detail="No concrete attestation recorded.",
        remediation="Record proof as a comment: the change id, the observed outcome, and when.",
    )
    assert f.remediation and "proof" in f.remediation.lower()

    # Optional — defaults to None when omitted.
    g = Finding(criterion="add --json flag", detail="flag absent in src/cli.py")
    assert g.remediation is None


def test_remediation_round_trips_through_serialized_verdict() -> None:
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    verdict = Model(
        verdict="FAIL",
        findings=[
            {
                "criterion": "[operator-attested] SNS subscription confirmed",
                "detail": "No attestation naming a confirmation id/outcome.",
                "remediation": (
                    "Record the confirmation: the subscription ARN and the delivery test outcome."
                ),
            }
        ],
    )
    dumped = verdict.model_dump()
    assert dumped["findings"][0]["remediation"].startswith("Record the confirmation")
