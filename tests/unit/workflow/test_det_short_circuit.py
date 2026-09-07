"""Verify DET blocking findings short-circuit plan review before the LLM tier.

A blocked plan invokes neither finder nor agent, and its verdict retains every deterministic
blocking finding. A DET-passing plan runs the LLM passes. The suite imports canned fixtures from
`test_plan_review_workflow.py` and makes no network calls.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import context_assembly

from .test_plan_review_workflow import (
    _GOOD_AC,
    _TARGET,
    _CannedAgent,
    _CountingFinder,
    _patch_reads,
    _run,
    _state,
    _terminal_verdict,
)

pytestmark = pytest.mark.unit

_NO_AC_PLAN = "Just a body: no acceptance-criteria checklist anywhere in this plan."


def _run_short_circuit(monkeypatch, state):
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)
    assert res.status == "succeeded", res.error
    return finder, canned, _terminal_verdict(rec)


def test_det_block_short_circuits_with_no_llm_calls(monkeypatch):
    """A P1 DET block (no `## Acceptance Criteria`) → BLOCK, zero LLM invocations."""
    finder, canned, verdict = _run_short_circuit(monkeypatch, _state(description=_NO_AC_PLAN))
    assert finder.calls == 0, "the Pass-1 finder must never run on a DET block"
    assert canned.calls == 0, "no verify/coach agent step may run on a DET block"
    assert verdict is not None
    assert verdict["verdict"] == "BLOCK"
    assert verdict["coverage"]["llm_ran"] is False


def test_short_circuit_verdict_carries_every_det_blocking_finding(monkeypatch):
    """The BLOCK verdict itemizes ALL DET blocking findings the floor produced — the same
    set (by minted finding id) partition_findings yields from the raw DET results — with
    the standard remediation guidance attached."""
    from rebar.llm.plan_review import det_floor, orchestrator

    state = _state(description=_NO_AC_PLAN)
    _patch_reads(monkeypatch, state)
    pctx = context_assembly.assemble_context(_TARGET, repo_root=None)
    det_results = det_floor.run_det_floor(pctx)
    expected = orchestrator.partition_findings(
        det_floor.det_blocking_findings(det_results),
        det_floor.det_advisory_findings(det_results),
        [],
        advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP,
    )["blocking"]
    assert expected, "fixture must produce at least one DET blocking finding"

    _finder, _canned, verdict = _run_short_circuit(monkeypatch, state)
    assert verdict is not None and verdict["verdict"] == "BLOCK"
    got_ids = {f["id"] for f in verdict["blocking"]}
    assert {f["id"] for f in expected} <= got_ids, "every DET block must reach the verdict"
    for f in verdict["blocking"]:
        assert f["tier"] == "DET"
        assert f.get("suggested_fix"), "each DET block must carry its remediation text"


def test_det_passing_plan_still_runs_llm_passes(monkeypatch):
    """A plan that clears the DET floor runs the LLM tier exactly as before."""
    finder, _canned, verdict = _run_short_circuit(monkeypatch, _state(description=_GOOD_AC))
    assert finder.calls > 0, "a DET-passing plan must still reach the Pass-1 finder"
    assert verdict is not None
    assert verdict["verdict"] == "PASS"
    assert verdict["coverage"]["llm_ran"] is not False
