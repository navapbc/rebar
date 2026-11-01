"""A misplaced [non-codebase] tag is reported, not silently ignored (bug ef13).

ADR 0043 recognizes ``[non-codebase]`` / ``[operator-attested]`` only at the START of the
checkbox text, and a tag anywhere else fails safe to the codebase bar. That rule stays. What
was missing is visibility: a trailing or mid-line tag produced no plan-review finding and no
close-precheck hint, so the author found out from an unexpected completion FAIL. These tests
pin the advisory plan-review finding and the close-precheck paragraph, and pin that a
correctly placed tag and a backticked mention are left alone.
"""

from __future__ import annotations

import pytest

from rebar._commands import txn
from rebar._commands._seam import CommandError
from rebar.llm.plan_review import det_operator_attested
from rebar.llm.plan_review.det_advisory import p6_ac_quality
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.det_operator_attested import ac_item_lines

TRAILING = "- [x] The change has `Verified +1` on Gerrit. [non-codebase]"
MID_LINE = "- [ ] AC4 [non-codebase]: the box journal shows the new container."
BOLD_TRAILING = "- [ ] An off-repo backup bundle exists. **[operator-attested]**"
BEFORE_BOX = "- [operator-attested] [x] The live test asserts echo safety."
PLACED = [
    "- [ ] [non-codebase] The change lands on main with Verified +1.",
    "- [x] [NON-CODEBASE] The change lands on main with Verified +1.",
    "- [ ] [operator-attested] The change lands on main with Verified +1.",
]
BACKTICKED = (
    "- [ ] The guide documents the `[non-codebase]` tag and its `- [ ] [non-codebase] …` form."
)


def _ac(*lines: str) -> str:
    head = "## Approach\n\nDo it.\n\n## Testing\n\npytest.\n\n## Acceptance Criteria\n\n"
    return head + "\n".join(lines) + "\n"


@pytest.mark.parametrize("line", [TRAILING, MID_LINE, BOLD_TRAILING, BEFORE_BOX])
def test_a_tag_away_from_the_start_is_flagged(line: str) -> None:
    assert det_operator_attested.misplaced_tag_lines(ac_item_lines(_ac(line))) == [line]


@pytest.mark.parametrize(
    "line", [*PLACED, "- [ ] [non-codebase]", BACKTICKED, "- [ ] Plain codebase criterion."]
)
def test_a_placed_tag_or_a_backticked_mention_is_not_flagged(line: str) -> None:
    assert det_operator_attested.misplaced_tag_lines(ac_item_lines(_ac(line))) == []


def _p6(description: str):
    ctx = PlanContext(
        ticket_id="ef13-6259-00f3-406d",
        ticket_type="bug",
        title="t",
        description=description,
        state={"file_impact": []},
        children=[],
    )
    return p6_ac_quality(ctx)


def _misplaced_issues(result) -> list[str]:
    issues = (result.finding or {}).get("evidence") or []
    return [i for i in issues if "start of the checkbox text" in i]


def test_plan_review_reports_a_trailing_tag_as_an_advisory_finding() -> None:
    result = _p6(_ac(TRAILING))
    found = _misplaced_issues(result)
    assert len(found) == 1, result
    assert "Verified +1" in found[0]
    assert "- [ ] [non-codebase]" in found[0]
    assert result.blocking is False
    assert result.coverage["misplaced_tag_gaps"] == 1


def test_plan_review_is_silent_on_a_backticked_mention() -> None:
    assert _misplaced_issues(_p6(_ac(BACKTICKED, *PLACED))) == []


def _close_error(*lines: str) -> str:
    with pytest.raises(CommandError) as exc:
        txn.ensure_ac_boxes_checked(
            "ef13-6259-00f3-406d", "/nonexistent", ticket_state={"description": _ac(*lines)}
        )
    return str(exc.value)


def test_close_precheck_names_a_misplaced_tag_on_an_unchecked_line() -> None:
    line = "- [ ] The change has `Verified +1` on Gerrit. [non-codebase]"
    msg = _close_error(line)
    assert line in msg
    assert "start of the checkbox text" in msg
    assert "rebar review-plan" in msg


def test_close_precheck_message_is_unchanged_for_an_untagged_line() -> None:
    msg = _close_error("- [ ] Plain codebase criterion.")
    assert "start of the checkbox text" not in msg


def test_a_correctly_placed_unchecked_tag_stays_exempt() -> None:
    txn.ensure_ac_boxes_checked(
        "ef13-6259-00f3-406d", "/nonexistent", ticket_state={"description": _ac(PLACED[0])}
    )
