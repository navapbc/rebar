"""CLI and MCP agree on whether a passed but unsigned plan review is retryable.

A persistence error is retryable because claim needs the missing signature. A deliberate
no-sign result carries a reason, not an error, and must not trigger a sign-review retry.
The tests assert matching observable results across both surfaces.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar._cli import _llm_commands as cli
from rebar.llm.plan_review.resign import classify_plan_review_attestation


def _result(**signature: Any) -> dict[str, Any]:
    """A PASS result carrying the given `signature` block."""
    return {"verdict": "PASS", "ticket_id": "t", "signature": dict(signature)}


# (label, signature block, expect_retryable)
CASES = [
    ("signed", {"signed": True}, False),
    ("deliberate skip", {"signed": False, "reason": "sign=False requested"}, False),
    ("sign failed", {"signed": False, "error": "disk full"}, True),
    (
        "plan changed",
        {
            "signed": False,
            "error": "the plan changed",
            "event": "plan_review_generation_changed",
        },
        True,
    ),
    ("no signature block", {}, False),
]


@pytest.mark.parametrize(("label", "sig", "expect_retryable"), CASES, ids=[c[0] for c in CASES])
def test_classifier_retryability(label: str, sig: dict, expect_retryable: bool) -> None:
    got = classify_plan_review_attestation(_result(**sig))
    assert got.retryable is expect_retryable, f"{label}: {got.as_dict()}"


@pytest.mark.parametrize(("label", "sig", "expect_retryable"), CASES, ids=[c[0] for c in CASES])
def test_cli_and_mcp_never_disagree(
    label: str, sig: dict, expect_retryable: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parity property the 'one implementation' requirement exists to produce.

    CLI signals retryable as exit 11; MCP signals it as `attestation.retryable`. Drive the
    SAME result through both and require the same answer.
    """
    from rebar._mcp_llm import _with_attestation

    mcp_payload = _with_attestation(dict(_result(**sig)), classify_plan_review_attestation)
    mcp_retryable = mcp_payload["attestation"]["retryable"]

    cli_exit = cli._disposition_exit_code(dict(_result(**sig)), indeterminate_code=2)
    capsys.readouterr()

    assert mcp_retryable is expect_retryable, f"{label}: MCP said {mcp_retryable}"
    assert (cli_exit == 11) is expect_retryable, f"{label}: CLI exit {cli_exit}"
    assert (cli_exit == 11) is mcp_retryable, (
        f"{label}: surfaces disagree — CLI exit {cli_exit}, MCP retryable {mcp_retryable}"
    )


# ── the recovery an agent is told to run must be the RIGHT one ──────────────


def test_sign_failure_recovers_via_sign_review() -> None:
    """A transient persist failure: the verdict is still valid, so re-signing is the cheap
    correct move (no LLM)."""
    got = classify_plan_review_attestation(_result(signed=False, error="disk full"))
    assert got.recovery_tool == "sign_review"


def test_plan_changed_does_not_recommend_sign_review() -> None:
    """Bug 94a3's rule: when the plan CHANGED, the computed verdict no longer describes the
    ticket, so `sign_review` would refuse and the agent would loop. It must be sent back to
    `review_plan` for a fresh verdict instead."""
    # The discriminator is the signature's `event`, not the error prose -- keying on the
    # message text would be a change-detector that any rewording breaks.
    got = classify_plan_review_attestation(
        _result(
            signed=False,
            error="the plan changed",
            event="plan_review_generation_changed",
        )
    )
    assert got.recovery_tool != "sign_review", (
        "recommending sign_review after a plan change sends the agent into a refusal loop"
    )
    assert got.recovery_tool == "review_plan"


def test_a_signed_result_names_no_recovery() -> None:
    got = classify_plan_review_attestation(_result(signed=True))
    assert got.recovery_tool is None
    assert got.retryable is False


# ── the MCP contract an agent branches on ───────────────────────────────────


def test_mcp_attaches_attestation_even_on_the_happy_path() -> None:
    """Present unconditionally, so an agent can branch without probing for the key."""
    from rebar._mcp_llm import _with_attestation

    payload = _with_attestation(_result(signed=True), classify_plan_review_attestation)
    att = payload["attestation"]
    for key in ("signed", "retryable", "cause", "recovery_tool", "message"):
        assert key in att, f"agents branch on {key!r}; it must always be present"


def test_mcp_payload_is_json_safe() -> None:
    """It crosses the MCP wire, so every value must serialize."""
    import json

    from rebar._mcp_llm import _with_attestation

    payload = _with_attestation(
        _result(signed=False, error="disk full"), classify_plan_review_attestation
    )
    json.dumps(payload["attestation"])


def test_mcp_does_not_disturb_the_rest_of_the_result() -> None:
    """Additive only — the verdict an existing agent already reads must survive."""
    from rebar._mcp_llm import _with_attestation

    payload = _with_attestation(_result(signed=True), classify_plan_review_attestation)
    assert payload["verdict"] == "PASS"
    assert payload["ticket_id"] == "t"
