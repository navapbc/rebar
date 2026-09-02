"""The plan-review guide documents the bug claim-time-exemption / post-RCA review
SEQUENCE explicitly (ticket 852a-1e41-632d-4087).

`docs/plan-review-gate.md` already documented two true facts separately -- a bug
gets a light advisory review tier (not a blanket exemption), and the CLI claim-time
exemption is a distinct, unchanged axis -- but never stated the operational
sequence: claim without plan review to do root-cause analysis (RCA) first, then a
COMPLEX bug (one whose RCA yields an implementation plan + file_impact naming
non-test paths) must pass full LLM plan review before implementation begins, while
a SIMPLE bug may proceed through the light tier. These tests pin the corrected,
explicit sequence language.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = REPO_ROOT / "docs" / "plan-review-gate.md"


def _guide_text() -> str:
    return _GUIDE.read_text(encoding="utf-8")


# ─────────────────────────── HAPPY PATH ──────────────────────────────────────


def test_guide_states_bug_may_be_claimed_without_review_for_rca():
    text = _guide_text()
    assert "claim a bug without plan review to perform root-cause" in text


def test_guide_states_complex_bug_needs_review_before_implementation():
    text = _guide_text()
    assert "must pass the full, blocking-capable LLM plan review" in text
    assert "before implementation begins" in text


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_guide_does_not_imply_every_bug_needs_review_before_claim():
    """The sequence text explicitly disclaims a blanket pre-claim review
    requirement -- a bug still needs no signed attestation to be claimed."""
    text = _guide_text()
    assert (
        "Neither case implies every bug needs review before claim" in text
        or "still needs no signed attestation\nto be *claimed*" in text
    )


def test_guide_does_not_imply_permanent_exemption_for_complex_remediation():
    text = _guide_text()
    assert "nor that a complex bug's remediation stays exempt" in text


def test_guide_distinguishes_simple_from_complex_bug_disposition():
    """A simple (test-only blast-radius) bug is NOT forced through the blocking
    rubric -- only a complex one (non-test blast radius) is."""
    text = _guide_text()
    assert "A **simple**\nbug (blast radius stays test-only) may proceed" in text


def test_claim_time_exemption_type_registry_includes_bug():
    """The CLI claim-time exemption (`_PLAN_REVIEW_EXEMPT_TYPES`) DOES include
    'bug' -- this is the structural mechanism behind the claim-without-review-for-
    RCA sequence documented above. The RCA-sequence doc change is doc-only and
    does not touch this registry."""
    gates_src = (REPO_ROOT / "src" / "rebar" / "_commands" / "gates.py").read_text(encoding="utf-8")
    assert '_PLAN_REVIEW_EXEMPT_TYPES = ("bug"' in gates_src
