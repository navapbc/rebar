"""Unit tests for rebar.grounding.evidence — the three-valued evidence contract.

Pins the contract: refute/match/abstain are representable in ONE shape; the reason
enum is CLOSED (incl. version_skew, invalid_detector, other); a resolved record
never carries a reason and an abstain always does; every built record validates
against the canonical JSON Schema (schemas.GROUNDING).
"""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.grounding import evidence as ev

pytestmark = pytest.mark.unit


# ── closed vocabularies ──────────────────────────────────────────────────────


def test_reason_enum_is_closed_and_matches_schema() -> None:
    schema = schemas.load(schemas.GROUNDING)
    schema_reasons = set(schema["$defs"]["abstain_reason"]["enum"])
    assert ev.ABSTAIN_REASONS == schema_reasons
    # The load-bearing first-class reasons + the explicit catch-all are present.
    for required in ("version_skew", "invalid_detector", "other"):
        assert required in ev.ABSTAIN_REASONS


def test_outcome_job_tier_vocabularies_match_schema() -> None:
    schema = schemas.load(schemas.GROUNDING)
    assert ev.OUTCOMES == set(schema["$defs"]["outcome"]["enum"])
    assert ev.JOBS == set(schema["$defs"]["job"]["enum"])
    assert ev.TIERS == set(schema["$defs"]["provenance_tier"]["enum"])


# ── builders → schema-valid, correct shape ───────────────────────────────────


def test_refuted_record_is_schema_valid_and_carries_no_reason() -> None:
    cov = ev.coverage(backend="ctags", status=ev.STATUS_RAN, version="6.2.1")
    rec = ev.refuted(
        provenance_tier=ev.TIER_T1,
        coverage=cov,
        reference={"kind": "symbol", "name": "TicketStore"},
        location={"file": "pkg/core.py", "line_start": 1},
    )
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["job"] == ev.JOB_REFUTE
    assert "reason" not in rec  # resolved records carry no reason
    assert ev.is_resolved(rec)
    ev.validate(rec)


def test_match_record_is_schema_valid() -> None:
    cov = ev.coverage(backend="opengrep", status=ev.STATUS_RAN, version="1.2.3")
    rec = ev.match(
        job=ev.JOB_SMELL,
        provenance_tier=ev.TIER_T1,
        coverage=cov,
        detector_id="rebar.builtin.smell.console-log",
        location={"file": "a.js", "line_start": 1},
        attention_only=True,
        detail="console.log smell",
    )
    assert rec["outcome"] == ev.OUTCOME_MATCH
    assert rec["attention_only"] is True
    ev.validate(rec)


def test_abstain_synthesizes_skipped_coverage_and_validates() -> None:
    rec = ev.abstain(
        "timeout",
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T1,
        backend="ctags",
        version="6.2.1",
        detail="exceeded 60s",
    )
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "timeout"
    assert rec["coverage"] == {"backend": "ctags", "status": "skipped", "version": "6.2.1", "reason": "timeout"}
    assert not ev.is_resolved(rec)
    ev.validate(rec)


@pytest.mark.parametrize("reason", sorted(ev.ABSTAIN_REASONS))
def test_every_closed_reason_builds_a_valid_abstain(reason: str) -> None:
    rec = ev.abstain(reason, job=ev.JOB_APPLIES, provenance_tier=ev.TIER_T0, backend="registry")
    assert rec["reason"] == reason
    ev.validate(rec)


# ── strict constructors reject contract violations ───────────────────────────


def test_abstain_rejects_unknown_reason() -> None:
    with pytest.raises(ev.GroundingContractError):
        ev.abstain("definitely-not-a-reason", job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1, backend="x")


def test_coverage_skipped_requires_reason_ran_forbids_it() -> None:
    with pytest.raises(ev.GroundingContractError):
        ev.coverage(backend="x", status="skipped")  # no reason
    with pytest.raises(ev.GroundingContractError):
        ev.coverage(backend="x", status="ran", reason="timeout")  # reason on a ran record


def test_resolved_rejects_unknown_job_and_tier() -> None:
    cov = ev.coverage(backend="x", status=ev.STATUS_RAN)
    with pytest.raises(ev.GroundingContractError):
        ev.refuted(job="nope", provenance_tier=ev.TIER_T1, coverage=cov)
    with pytest.raises(ev.GroundingContractError):
        ev.match(job=ev.JOB_SMELL, provenance_tier="T9", coverage=cov)


# ── lenient normalization clamps, never raises ───────────────────────────────


def test_normalize_clamps_unknown_outcome_and_reason() -> None:
    rec = ev.normalize_evidence({"outcome": "weird", "reason": "also-weird", "coverage": {"backend": "z"}})
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN  # unknown outcome → abstain
    assert rec["reason"] == "other"  # unknown reason → other
    ev.validate(rec)


def test_normalize_strips_reason_from_resolved_record() -> None:
    rec = ev.normalize_evidence(
        {"outcome": "match", "job": "smell", "provenance_tier": "T1", "reason": "timeout", "coverage": {"backend": "z", "status": "ran"}}
    )
    assert "reason" not in rec  # _drop_nulls removes the nulled reason
    ev.validate(rec)


def test_normalize_synthesizes_missing_coverage() -> None:
    rec = ev.normalize_evidence({"outcome": "abstain", "reason": "no_tool"})
    assert rec["coverage"]["status"] == "skipped"
    assert rec["coverage"]["reason"] == "no_tool"
    ev.validate(rec)
