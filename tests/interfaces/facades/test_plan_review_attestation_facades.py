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
