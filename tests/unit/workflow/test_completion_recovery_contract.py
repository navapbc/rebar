"""Visible unit contracts for bounded completion recovery."""

from __future__ import annotations

import pytest

from rebar.llm.errors import CompletionRecoveryError
from rebar.llm.workflow.completion_recovery import (
    _validate_coverage,
    _validate_recovery_inputs,
    explicit_completion_criteria,
)


def test_pass_like_verdict_cannot_hide_an_unmet_criterion() -> None:
    result = {
        "verdict": "  pass ",
        "criteria": [{"criterion": "Ship the fix", "met": False}],
    }

    with pytest.raises(CompletionRecoveryError, match="unmet criterion"):
        _validate_coverage(result, ["Ship the fix"])


def test_plan_checkbox_is_not_a_completion_criterion() -> None:
    ticket = {
        "ticket_type": "task",
        "title": "Document behavior",
        "description": (
            "## Acceptance Criteria\n"
            "The behavior is documented clearly.\n\n"
            "## Plan\n"
            "- [ ] Run tests\n"
        ),
    }

    with pytest.raises(CompletionRecoveryError, match="cannot enumerate"):
        explicit_completion_criteria(ticket)


def test_bug_without_completion_checklist_uses_only_bug_core() -> None:
    ticket = {
        "ticket_type": "bug",
        "title": "Output cap",
        "description": "## Plan\n- [ ] Run tests\n",
    }

    assert explicit_completion_criteria(ticket) == [
        "Bug 'Output cap' is actually resolved: the reported defect no longer "
        "reproduces and expected behavior holds."
    ]


def test_authored_bug_core_is_not_appended_twice_at_criteria_bound() -> None:
    core = (
        "Bug 'Output cap' is actually resolved: the reported defect no longer "
        "reproduces and expected behavior holds."
    )
    authored = [f"Criterion {index}" for index in range(31)] + [core]
    ticket = {
        "ticket_type": "bug",
        "title": "Output cap",
        "description": "## Acceptance Criteria\n"
        + "\n".join(f"- [ ] {criterion}" for criterion in authored),
    }

    criteria = explicit_completion_criteria(ticket)

    assert criteria == authored
    assert criteria.count(core) == 1
    _validate_recovery_inputs(criteria, "", None)


def test_acceptance_criteria_stay_ordered_deduped_and_accept_all_met() -> None:
    ticket = {
        "ticket_type": "task",
        "title": "Bound recovery",
        "description": (
            "## Plan\n"
            "- [ ] Ignore this\n\n"
            "## Acceptance Criteria\n"
            "- [ ] First\n"
            "- [x] Second\n"
            "- [ ] First\n\n"
            "## Notes\n"
            "- [ ] Ignore this too\n"
        ),
    }
    expected = ["First", "Second"]

    assert explicit_completion_criteria(ticket) == expected
    _validate_coverage(
        {
            "verdict": "PASS",
            "criteria": [
                {"criterion": "First", "met": True},
                {"criterion": "Second", "met": True},
            ],
        },
        expected,
    )
