"""Emit-side prompt cutover to `[non-codebase]` (story f371, ADR 0101) — happy path.

The reviewer prompts that TELL the model which tag to emit must name the canonical spelling,
and the coach move authors see must match. Recognition-side prompts are covered separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

EMIT_PROMPTS = [
    "src/rebar/llm/reviewers/plan_review_E2.md",
    "src/rebar/llm/reviewers/plan_review_E6.md",
    "src/rebar/llm/reviewers/plan_review_F1.md",
]


@pytest.mark.parametrize("path", EMIT_PROMPTS)
def test_emit_prompts_name_the_canonical_tag(path: str) -> None:
    """Each emit-side rule instructs the canonical `[non-codebase]` spelling."""
    assert "[non-codebase]" in (REPO / path).read_text()


def test_coach_move_14_template_uses_the_canonical_tag() -> None:
    """Move 14 is the coaching an author actually reads, so it must teach the new tag."""
    from rebar.llm.plan_review.coach_moves import MOVE_REGISTRY

    assert "[non-codebase]" in MOVE_REGISTRY["14"]["template"]
    assert "[operator-attested]" not in MOVE_REGISTRY["14"]["template"]


def test_generated_criteria_guide_is_in_parity() -> None:
    """The guide is GENERATED from these prompts; editing them without regenerating leaves
    the shipped guide contradicting the rubric the model actually runs."""
    from rebar.llm.plan_review import registry

    assert registry.validate_criteria_guide(str(REPO)) == []
