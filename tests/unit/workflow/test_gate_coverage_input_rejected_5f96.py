"""Tests distinct degradation signals for LLM availability and input rejection.

``coverage.llm_unavailable`` marks retryable availability faults. Deterministic
``LLMInputRejectedError`` failures instead set ``coverage.input_rejected`` so consumers fail
closed without treating them as outages. This happy-path oracle covers
``degrade_cause_flags`` and the code-review degrade builder. Companion tests cover edge,
end-to-end, and reader behavior.

Held-out oracles merged from test_gate_coverage_input_rejected_5f96_heldout: 5f96 — HELD-OUT
oracle (edge / E2E / the three readers). Not shown to the implementer.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError

pytestmark = pytest.mark.unit


def test_degrade_cause_flags_splits_availability_from_input_rejection() -> None:
    """The single writer of the cause flags: a genuine outage flags ``llm_unavailable`` (and
    NOT ``input_rejected``); a deterministic ``LLMInputRejectedError`` flags ``input_rejected``
    (and NOT ``llm_unavailable``). Exactly one is True — they are mutually exclusive."""
    from rebar.llm.failure import degrade_cause_flags

    assert degrade_cause_flags(LLMUnavailableError("overloaded")) == {
        "llm_unavailable": True,
        "input_rejected": False,
    }
    assert degrade_cause_flags(LLMInputRejectedError("prompt too large")) == {
        "llm_unavailable": False,
        "input_rejected": True,
    }


def test_degraded_code_review_verdict_flags_input_rejected_distinctly() -> None:
    """The code-review degrade builder must carry the SPLIT signal: an input-rejection degrade
    sets ``input_rejected`` True / ``llm_unavailable`` False (still an INDETERMINATE, never a
    hollow PASS), while a plain outage keeps the classic ``llm_unavailable`` True /
    ``input_rejected`` False."""
    from rebar.llm.workflow.gate_dispatch import _degraded_code_review_verdict

    rejected = _degraded_code_review_verdict(
        error=LLMInputRejectedError("prompt too large"), runner_name="r"
    )
    assert rejected["verdict"] == "INDETERMINATE"
    assert rejected["coverage"]["input_rejected"] is True
    assert rejected["coverage"]["llm_unavailable"] is False

    outage = _degraded_code_review_verdict(error=LLMUnavailableError("overloaded"), runner_name="r")
    assert outage["verdict"] == "INDETERMINATE"
    assert outage["coverage"]["llm_unavailable"] is True
    assert outage["coverage"]["input_rejected"] is False


# ── held-out oracles (merged from test_gate_coverage_input_rejected_5f96_heldout) ──


def test_degrade_cause_flags_string_error_defaults_to_unavailable() -> None:
    """A string error (the finders-produced-nothing tail of the plan-review degrade path) is
    NOT an ``LLMInputRejectedError`` → it keeps the classic ``llm_unavailable`` True, so the
    split never silently drops the availability signal for the non-exception path."""
    from rebar.llm.failure import degrade_cause_flags

    assert degrade_cause_flags("finders produced nothing") == {
        "llm_unavailable": True,
        "input_rejected": False,
    }


def _ctx():
    from rebar.llm.plan_review.det_floor import PlanContext

    return PlanContext(
        ticket_id="T-5f96",
        ticket_type="story",
        title="Split coverage signal",
        description=(
            "## Why\nHonesty.\n\n## What\nSplit the flag.\n\n"
            "## Acceptance Criteria\n- [ ] input rejection is distinct (`pytest -q`)\n"
        ),
    )


def _cfg(tmp_path):
    from rebar.llm.config import LLMConfig

    return dataclasses.replace(
        LLMConfig(runner="fake"), model="claude-haiku-4-5", repo_path=str(tmp_path)
    )


def test_degraded_plan_review_verdict_flags_input_rejected_e2e(tmp_path) -> None:
    """End-to-end through the plan-review degrade builder (DET floor runs, LLM did not): an
    ``LLMInputRejectedError`` degrade yields an INDETERMINATE verdict whose coverage carries the
    split (``input_rejected`` True / ``llm_unavailable`` False), while a string-error tail keeps
    the classic ``llm_unavailable`` True."""
    from rebar.llm.workflow.plan_review_recovery import _degraded_plan_review_verdict

    rejected = _degraded_plan_review_verdict(
        _ctx(),
        _cfg(tmp_path),
        error=LLMInputRejectedError("prompt too large"),
        advisory_cap=10,
        runner_name="fake",
    )
    assert rejected["verdict"] == "INDETERMINATE"
    assert rejected["coverage"]["input_rejected"] is True
    assert rejected["coverage"]["llm_unavailable"] is False

    tail = _degraded_plan_review_verdict(
        _ctx(),
        _cfg(tmp_path),
        error="finders produced nothing",
        advisory_cap=10,
        runner_name="fake",
    )
    assert tail["coverage"]["llm_unavailable"] is True
    assert tail["coverage"]["input_rejected"] is False


def _finalize(coverage):
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    return orchestrator.finalize_verdict(
        PlanContext(ticket_id="abcd-0000-0000-0001", ticket_type="task", title="", description=""),
        {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []},
        coaching=[],
        coverage=coverage,
        runner_name="fake",
        model="m",
    )


def test_finalize_verdict_input_rejected_is_indeterminate() -> None:
    """The plan-review terminal reader must degrade an input-rejection coverage gap to
    INDETERMINATE (never a hollow PASS) — the same posture it already takes for an outage."""
    assert (
        _finalize({"llm_ran": False, "input_rejected": True, "llm_unavailable": False})["verdict"]
        == "INDETERMINATE"
    )
    # Control: a fully clean coverage with no gap flag still PASSes — the split did not make the
    # reader over-eager.
    assert _finalize({"llm_ran": True})["verdict"] == "PASS"


def test_recompute_verdict_after_drop_downgrades_on_input_rejected() -> None:
    """After the drift floor empties the blocking bucket, a BLOCK riding an input-rejection
    coverage gap must re-derive to INDETERMINATE, exactly as it already does for an outage."""
    from rebar.llm.plan_review import drift_floor

    v = {"verdict": "BLOCK", "blocking": [], "coverage": {"input_rejected": True}}
    drift_floor._recompute_verdict_after_drop(v)
    assert v["verdict"] == "INDETERMINATE"

    # Control: the classic outage flag still downgrades (unchanged behavior).
    outage = {"verdict": "BLOCK", "blocking": [], "coverage": {"llm_unavailable": True}}
    drift_floor._recompute_verdict_after_drop(outage)
    assert outage["verdict"] == "INDETERMINATE"


def test_adapter_input_rejected_is_distinct_nonretryable_gap() -> None:
    """The review-bot must map an input-rejection coverage gap to its own ``input-rejected``
    sub-reason, and that reason must be NON-retryable: input rejection is deterministic, so the
    voter casts an honest fail-closed veto rather than deferring vote-less for a backfill retry
    (which would loop forever on the same oversized/blocked input)."""
    from rebar.review_bot import adapter

    assert adapter._coverage_gap_reason({"input_rejected": True}) == "input-rejected"
    # Control: the availability flag still maps to the retryable outage reason.
    assert adapter._coverage_gap_reason({"llm_unavailable": True}) == "llm-unavailable"

    # The decision the voter acts on: a BLOCK carrying the input-rejected gap_reason.
    verdict = {"verdict": "INDETERMINATE", "blocking": [], "coverage": {"input_rejected": True}}
    decision = adapter._block("input-rejected", verdict)
    assert decision["decision"] == "BLOCK"
    assert decision["gap_reason"] == "input-rejected"
    assert decision["coverage_gap"] is True
    assert "input-rejected" in decision["message"]

    # The retryability split: outage/scanner/etc. defer vote-less; input-rejected does NOT.
    assert "input-rejected" not in adapter.RETRYABLE_GAP_REASONS
    assert "llm-unavailable" in adapter.RETRYABLE_GAP_REASONS
