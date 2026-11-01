"""Held-out scope-enumeration contract for completion recovery."""

from __future__ import annotations

import pytest

from rebar.llm.errors import CompletionRecoveryError
from rebar.llm.workflow.completion_recovery import explicit_completion_criteria

pytestmark = pytest.mark.unit


def test_recovery_never_substitutes_a_plan_checkbox_for_prose_acceptance() -> None:
    ticket = {
        "ticket_id": "T-mixed",
        "title": "preserve exported links",
        "ticket_type": "task",
        "description": (
            "## Acceptance Criteria\n"
            "Exported tickets preserve every dependency link during a round trip.\n\n"
            "## Plan\n"
            "- [ ] Run the focused tests\n"
        ),
    }

    try:
        criteria = explicit_completion_criteria(ticket)
    except CompletionRecoveryError:
        return  # fail-closed is valid when prose cannot be enumerated safely

    assert any("preserve every dependency link" in item for item in criteria)
    assert all("Run the focused tests" not in item for item in criteria)


def test_bug_resolution_core_is_enumerated_exactly_once() -> None:
    core = (
        "Bug 'bounded completion' is actually resolved: the reported defect no longer "
        "reproduces and expected behavior holds."
    )
    ticket = {
        "ticket_id": "T-bug",
        "title": "bounded completion",
        "ticket_type": "bug",
        "description": f"## Acceptance Criteria\n- [ ] {core}\n",
    }

    criteria = explicit_completion_criteria(ticket)

    assert criteria == [core]
