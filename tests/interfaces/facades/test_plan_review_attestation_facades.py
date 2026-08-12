"""CLI + MCP parity for the passed-but-unsigned plan-review state (ticket
``ammonic-amoral-nabarlek``).

A plan review that PASSED but whose attestation failed to persist is RETRYABLE, not success:
the signature the claim gate consumes was never written, so a following ``claim`` still
fails. That rule used to live only in the CLI, so an MCP agent saw ``verdict: PASS`` and
walked into a refused claim. The classifier now sits below both front-ends
(:func:`rebar.llm.plan_review.resign.classify_plan_review_attestation`).

These are HAPPY-PATH tests only — the normal passed-and-signed path plus the basic
passed-but-unsigned classification — and they assert OBSERVABLE behaviour: the CLI's exit
code and the exact stderr BYTES, and the MCP tool's returned payload fields. Offline: the
gate is monkeypatched at ``rebar.llm.review_plan`` (both surfaces call it by module
attribute), so no model, no network, no ``[agents]`` extra is exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
import rebar.llm

# The CLI's exit-11 stderr for a TRANSIENT sign failure, byte-for-byte. Pinned here (and not
# assembled from the production strings) so a reword of the message is a visible test diff
# rather than a silent contract change for the agents that read it.
_TRANSIENT_STDERR = (
    "plan review PASSED but the attestation could not be persisted: index.lock exists\n"
    "run `rebar sign-review {tid}` to re-sign from the recorded review "
    "(no LLM re-run) — the claim gate needs the signature.\n"
)


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "plan review attestation task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] the thing exists\n",
        repo_root=str(repo),
    )


def _verdict(ticket_id: str, signature: dict) -> dict:
    """A minimal PASS plan_review_verdict carrying the given ``signature`` block."""
    return {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "indeterminate": [],
        "coverage": {},
        "signature": signature,
    }


def _gate(signature: dict):
    def _run(ticket_id, **kw):
        return _verdict(ticket_id, signature)

    return _run


_SIGNED = {"signed": True, "key_id": "k1", "head_sha": "0" * 40}
_UNSIGNED_TRANSIENT = {
    "signed": False,
    "error": "index.lock exists",
    "event": "plan_review_generation_retry",
}


# ── CLI ─────────────────────────────────────────────────────────────────────────
def test_cli_passed_and_signed_is_exit_zero_and_silent(rebar_repo: Path, monkeypatch, capsys):
    """The normal path: a PASS whose attestation persisted stays exit 0 and says nothing
    about re-signing."""
    from rebar._cli import main

    tid = _seed(rebar_repo)
    monkeypatch.setattr(rebar.llm, "review_plan", _gate(_SIGNED))
    rc = main(["review-plan", tid, "-o", "json"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "PASSED but" not in err


def test_cli_passed_but_unsigned_is_exit_11_with_byte_identical_stderr(
    rebar_repo: Path, monkeypatch, capsys
):
    """The defect's CLI half, unchanged by the hoist: exit 11 and the EXACT stderr bytes."""
    from rebar._cli import main

    tid = _seed(rebar_repo)
    monkeypatch.setattr(rebar.llm, "review_plan", _gate(_UNSIGNED_TRANSIENT))
    rc = main(["review-plan", tid, "-o", "json"])
    err = capsys.readouterr().err
    assert rc == 11
    assert err == _TRANSIENT_STDERR.format(tid=tid)


# ── MCP ─────────────────────────────────────────────────────────────────────────
def _build_mcp():
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    return build_server()


def _call_review_plan(ticket_id: str) -> dict:
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path

    srv = _build_mcp()
    return _unwrap(asyncio.run(srv.call_tool("review_plan", {"ticket_id": ticket_id})))


def test_mcp_passed_and_signed_reports_a_non_retryable_attestation(rebar_repo: Path, monkeypatch):
    """The normal path still carries the structured block, so an agent can branch on one
    field unconditionally instead of testing for its presence."""
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setattr(rebar.llm, "review_plan", _gate(_SIGNED))
    tid = _seed(rebar_repo)

    res = _call_review_plan(tid)

    assert res["verdict"] == "PASS"
    assert res["attestation"]["signed"] is True
    assert res["attestation"]["retryable"] is False
    assert res["attestation"]["cause"] == "signed"
    assert res["attestation"]["recovery_tool"] is None


def test_mcp_passed_but_unsigned_is_flagged_retryable_and_names_sign_review(
    rebar_repo: Path, monkeypatch
):
    """The defect's MCP half: the agent must be able to see, WITHOUT parsing English, that
    this PASS left no signature and that ``sign_review`` is the recovery."""
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setattr(rebar.llm, "review_plan", _gate(_UNSIGNED_TRANSIENT))
    tid = _seed(rebar_repo)

    res = _call_review_plan(tid)

    assert res["verdict"] == "PASS"
    att = res["attestation"]
    assert att["signed"] is False
    assert att["retryable"] is True
    assert att["cause"] == "sign_failed"
    assert att["recovery_tool"] == "sign_review"
    assert att["error"] == "index.lock exists"


# ── sidecar lost (bug inborn-asbestine-moray) ───────────────────────────────────
# One contention episode can take BOTH the signature and the recovery sidecar. `sign-review`
# re-signs FROM that sidecar, so with none written it reads the PREVIOUS round's record and
# refuses — the advertised cheap recovery could not work, at the cost of a whole review.


def _classify(signature: dict, sidecar_emitted: object) -> dict:
    """The classifier's verdict for a PASS carrying ``signature`` and ``sidecar_emitted``."""
    from rebar.llm.plan_review.resign import classify_plan_review_attestation

    result = _verdict("t1", signature)
    if sidecar_emitted is not None:
        result["sidecar_emitted"] = sidecar_emitted
    return classify_plan_review_attestation(result).as_dict()


def test_sidecar_lost_names_review_plan_and_never_sign_review():
    """AC1: with no sidecar there is nothing to re-sign, so the guidance must not send the
    reader to `sign-review` — the dead end that cost two full reviews in the field."""
    att = _classify(_UNSIGNED_TRANSIENT, False)

    assert att["cause"] == "sidecar_lost"
    assert att["recovery_tool"] == "review_plan"
    assert att["retryable"] is True
    assert att["signed"] is False
    assert "sign-review" not in att["message"]
    assert "sign_review" not in att["message"]


def test_sidecar_present_keeps_the_cheap_no_llm_resign_guidance():
    """AC2: when only the signature was lost the recorded PASS still exists, so the cheap
    no-LLM `sign-review` recovery is still the right — and still the named — one."""
    att = _classify(_UNSIGNED_TRANSIENT, True)

    assert att["cause"] == "sign_failed"
    assert att["recovery_tool"] == "sign_review"
    assert "sign-review" in att["message"]


def test_sidecar_lost_message_marks_any_recorded_verdict_as_not_current():
    """AC3: the recorded verdict silently remains the PREVIOUS round's — a stale
    CONTRADICTING verdict. The message must say so rather than let it read as current."""
    message = _classify(_UNSIGNED_TRANSIENT, False)["message"]

    assert "NOT current" in message
    assert "predates this review" in message


@pytest.mark.parametrize(
    ("event", "error", "expected"),
    [
        ("plan_review_generation_changed", "the plan changed", "plan_changed"),
        ("plan_review_generation_retry", "index.lock exists", "sidecar_lost"),
    ],
)
def test_material_change_still_outranks_a_lost_sidecar(event, error, expected):
    """AC4: the new branch sits BELOW the material-change branch, so a stale plan is still
    reported as a stale plan — the lost sidecar must not shadow a sharper diagnosis."""
    att = _classify({"signed": False, "error": error, "event": event}, False)

    assert att["cause"] == expected
    assert att["recovery_tool"] == "review_plan"


def test_absent_sidecar_field_is_not_read_as_a_lost_sidecar():
    """Absence says nothing about the sidecar. Only an explicit False is evidence, so a
    result without the field keeps its prior classification rather than being downgraded."""
    att = _classify(_UNSIGNED_TRANSIENT, None)

    assert att["cause"] == "sign_failed"
    assert att["recovery_tool"] == "sign_review"


def test_cli_reports_the_lost_sidecar_instead_of_the_sign_review_dead_end(
    rebar_repo: Path, monkeypatch, capsys
):
    """The CLI half, end to end: still exit 11 (retryable), but the stderr an agent reads
    now names the recovery that can actually work."""
    from rebar._cli import main

    tid = _seed(rebar_repo)

    def _run(ticket_id, **kw):
        result = _verdict(ticket_id, _UNSIGNED_TRANSIENT)
        result["sidecar_emitted"] = False
        return result

    monkeypatch.setattr(rebar.llm, "review_plan", _run)
    rc = main(["review-plan", tid, "-o", "json"])
    err = capsys.readouterr().err

    assert rc == 11
    assert "sign-review" not in err
    assert f"rebar review-plan {tid}" in err
