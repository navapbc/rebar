"""T5c rubric pins ground security findings in the current code and configuration.

The criterion-local override permits at most one undeclared-access-posture finding per plan,
demotes ungrounded candidates to MINOR, and reserves major-or-higher severity for the
two HIGH classes. Existing severity-to-priority routing keeps lesser findings below
the 0.90 blocking threshold. Routing and Pass 3 are tested separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]
_RUBRIC = _ROOT / "src/rebar/llm/reviewers/plan_review_T5c.md"


def _text() -> str:
    return _RUBRIC.read_text(encoding="utf-8")


def test_rubric_carries_the_criterion_local_grounding_override() -> None:
    text = _text()
    assert "GROUNDING OVERRIDE (this criterion only)" in text
    # The override names the default stance it overrides, mandates tool-grounded
    # evaluation against the current tree, and states the flip rule.
    assert "evaluate-the-spec-AS-WRITTEN default" in text
    assert (
        "ground the plan's security implications in the CURRENT codebase and "
        "configuration via the read tools" in text
    )
    assert "FLIPS a finding" in text


def test_rubric_caps_undeclared_access_posture_findings_at_one_per_plan() -> None:
    text = _text()
    assert "at most ONE undeclared-access-posture finding per plan" in text
    # Extra surfaces fold into the single finding's evidence, not new findings.
    assert "fold the additional surfaces into that single finding's evidence" in text


def test_rubric_demotes_ungrounded_major_findings_to_minor_not_suppressed() -> None:
    text = _text()
    assert "GROUNDING DEMOTION" in text
    assert "neither a repo artifact you actually read" in text
    assert "quoted plan sentence" in text
    assert "pressed at MINOR" in text
    assert "demoted, never suppressed" in text


def test_rubric_scopes_severity_major_to_the_two_high_classes() -> None:
    text = _text()
    assert "ONLY these two HIGH classes warrant severity >= major" in text
    for phrase in (
        "plaintext secret on a boundary-crossing path",
        "undeclared internet-reachable sensitive surface",
    ):
        assert phrase in text, phrase
    assert "every other finding is at most minor" in text


def test_rewrite_preserves_the_pinned_trust_boundary_framing() -> None:
    # The 2e89 trust-boundary pins (tests/unit/test_plan_review.py) must survive the
    # rewrite; re-assert the load-bearing phrases here so a future edit of either
    # file fails close to the rubric.
    text = _text()
    for phrase in (
        "TRUST-BOUNDARY SCOPE GATE",
        "REACHABLE BY A LOWER-TRUST ACTOR",
        "MIXED-SCOPE",
        "SAME-ROUND DEDUP",
        "ZERO-TRUST CAVEAT",
        "no blurring",
    ):
        assert phrase in text, phrase
