"""T5c grounded-security rewrite rubric-text pins (ticket c97a).

The T5c security overlay reviews plans under the gate-wide reviewing stance
(``_SHARED_PREAMBLE`` in ``plan_review/passes.py``), whose "Evaluate the spec AS
WRITTEN" line suits spec criteria but not a security criterion — exploitability is a
property of the system, not of the text. Ticket c97a (operator-approved) gives the
rubric a criterion-LOCAL override plus three finder instructions, following the
bfa8 T10 precedent ("severity guidance, not new machinery"):

* GROUNDING OVERRIDE — evaluate against the CURRENT codebase/configuration via the
  read tools; a control defined elsewhere flips a finding;
* DEDUP-AT-SOURCE — at most ONE undeclared-access-posture finding per plan (extra
  surfaces fold into that finding's evidence);
* GROUNDING DEMOTION — a candidate finding citing neither a read repo artifact nor
  a quoted plan sentence is pressed at MINOR (demoted, never suppressed);
* HIGH-class scoping — ONLY the two HIGH classes warrant severity >= major, which
  keeps lesser findings below the 0.90 blocking bar through the existing
  severity->priority pipeline (no pass3_decide changes).

These are string pins on the rubric file, the same pattern as
``test_t10_rubric_contains_major_class_severity_guidance``; the routing/Pass-3
blocking mechanics are pinned in ``test_criteria_blocking.py``.
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
