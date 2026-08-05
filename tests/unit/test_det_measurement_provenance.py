"""Happy-path unit tests for the measurement-provenance DET lint (story f161, epic 3147).

Contract (story f161 §1a). An ``[operator-attested]`` AC item must carry a ``provenance:``
CONTINUATION LINE, indented under the checkbox item it belongs to, in one canonical form:

    - [ ] [operator-attested] <criterion text>
          provenance: environment=<v>; principal=<v>; privilege_posture=<production-equivalent|
          broader|narrower>; instrument=<live-call|simulation|static-analysis> — <justification>

This module is the DETERMINISTIC floor: it asserts the declaration is PRESENT and well-SHAPED
(all four keys, non-placeholder values, enum members, a justification). It makes NO judgement
about whether the declared values are CORRECT — that stays with the AGENT-tier criterion
``project.measurement-provenance``.
"""

from __future__ import annotations

from rebar.llm.plan_review.det_measurement_provenance import (
    provenance_gaps,
    provenance_issues,
)

_GOOD = (
    "provenance: environment=896586841071/us-east-1; "
    "principal=arn:aws:iam::896586841071:role/rebar-ci; "
    "privilege_posture=production-equivalent; instrument=live-call "
    "— the CI role is the role prod uses, so a missing grant fails here exactly as in prod"
)


def _plan(*lines: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(lines) + "\n"


def test_complete_declaration_has_no_gap() -> None:
    """A fully-declared [operator-attested] AC is clean — the lint must not nag a correct plan."""
    text = _plan(
        "- [ ] [operator-attested] the suite passes, evidenced by the Gerrit change id",
        f"      {_GOOD}",
    )
    assert provenance_gaps(text) == []
    assert provenance_issues(text) == []


def test_attested_item_with_no_declaration_is_flagged() -> None:
    """An [operator-attested] AC carrying NO provenance line at all is the core gap this
    lint exists to catch — the wrong-account and privilege-that-cannot-fail escapes both
    entered through exactly this hole."""
    text = _plan("- [ ] [operator-attested] the suite passes, evidenced by the Gerrit change id")
    gaps = provenance_gaps(text)
    assert len(gaps) == 1
    ac_line, reasons = gaps[0]
    assert "[operator-attested]" in ac_line
    assert reasons, "a gap must explain what is missing"
    issues = provenance_issues(text)
    assert len(issues) == 1
    assert "provenance" in issues[0].lower()
