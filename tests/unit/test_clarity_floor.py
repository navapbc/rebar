"""Deterministic hard floors shared by clarity-check and plan-review P1."""

from __future__ import annotations

import pytest

from rebar._engine_support.gates import clarity_check_compute
from rebar.llm.plan_review.det_floor import PlanContext, p1_readiness_shape

_FILLER = (
    "This ticket describes a cohesive implementation with enough concrete detail "
    "for an agent to execute it without guessing. " * 3
)


def _description(
    *,
    approach: str = "Update src/rebar/example.py through the existing gate seam.",
    scope: str = "Change the parser and its focused regression tests.",
    testing: str = "Run the focused unit tests.",
    acceptance_criteria: str = "- [ ] The focused regression test passes.",
) -> str:
    return (
        f"## Context\n{_FILLER}\n\n"
        f"## Approach\n{approach}\n\n"
        f"## Scope\n{scope}\n\n"
        f"## Testing\n{testing}\n\n"
        f"## Acceptance Criteria\n{acceptance_criteria}\n"
    )


def _p1(description: str):
    return p1_readiness_shape(
        PlanContext(
            ticket_id="clarity-floor-test",
            ticket_type="task",
            title="Clarity floor probe",
            description=description,
        )
    )


def _assert_both_fail(description: str) -> None:
    result, code = clarity_check_compute("task", description, threshold=5)
    assert code == 1
    assert result["verdict"] == "fail"
    p1 = _p1(description)
    assert p1.status == "fail"
    assert p1.blocked


def _assert_both_pass(description: str) -> None:
    result, code = clarity_check_compute("task", description, threshold=5)
    assert code == 0
    assert result["verdict"] == "pass"
    p1 = _p1(description)
    assert p1.status == "pass"
    assert not p1.blocked


def test_fully_joined_whitespace_only_ac_item_fails_both_surfaces() -> None:
    description = _description(
        acceptance_criteria="- [ ] A real outcome remains.\n- [ ]   \n    \t  "
    )
    _assert_both_fail(description)


def test_blank_checkbox_line_with_nonblank_continuation_passes() -> None:
    description = _description(
        acceptance_criteria="- [ ]\n  The wrapped continuation names the observable outcome."
    )
    _assert_both_pass(description)


def test_fenced_continuation_still_makes_an_ac_item_nonempty() -> None:
    description = _description(acceptance_criteria="- [ ]\n  ```text\n  observable artifact\n  ```")
    _assert_both_pass(description)


@pytest.mark.parametrize(
    "item",
    [
        "- [later] Decide the outcome.",
        "- [ ]Missing required whitespace after the marker.",
    ],
)
def test_nonstandard_checkbox_marker_does_not_satisfy_the_floor(item: str) -> None:
    _assert_both_fail(_description(acceptance_criteria=item))


@pytest.mark.parametrize(
    ("section", "line"),
    [
        ("approach", "TBD: choose the backend"),
        ("scope", "- TODO — decide the boundary"),
        ("testing", "backend = FIXME"),
        ("acceptance_criteria", "- [ ] The final status is ???"),
        ("acceptance_criteria", "- [ ] TBD: define the observable outcome"),
    ],
)
def test_sentinel_value_positions_fail_both_surfaces(section: str, line: str) -> None:
    kwargs = {section: line}
    _assert_both_fail(_description(**kwargs))


@pytest.mark.parametrize(
    "approach",
    [
        "The parser recognizes TODO markers in plan prose.",
        "Document `backend: TBD` as a literal example.",
        "```text\nbackend: TBD\n```",
        "~~~text\nbackend = FIXME\n~~~",
        "The status is currently TBD only in this explanatory counterexample.",
    ],
)
def test_non_assignment_and_code_examples_do_not_trigger(approach: str) -> None:
    _assert_both_pass(_description(approach=approach))


@pytest.mark.parametrize(
    "line",
    [
        "NO stale TODO",
        "backend: TBD; zero unresolved choices remain",
        "Remove backend: TBD markers",
        "Delete backend: TBD examples",
        "Reject backend: TBD as invalid input",
        "Forbid backend: TBD in submitted plans",
        "backend must not be TBD",
        "Proceed without backend: TBD placeholders",
    ],
)
def test_same_line_removal_and_negation_assertions_suppress_sentinel(line: str) -> None:
    _assert_both_pass(_description(approach=line))


def test_suppression_terms_match_whole_words_not_substrings() -> None:
    _assert_both_fail(_description(approach="implementation annotation; backend: TBD"))


def test_only_exact_active_section_names_are_scanned() -> None:
    description = _description() + "\n## Historical Approach\nbackend: TBD\n"
    _assert_both_pass(description)
