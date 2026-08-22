"""Contract for the rebar-debug Step 7 class-review directive (story dff2).

Step 7 sweeps for siblings of a proven root cause. Before this story it fixed
each sibling but never turned the confirmed sibling set into *declared,
reviewable* scope, so the fix for one instance was never reviewed as a design
for the family. The directive under contract here makes the sibling set:

* become per-sibling acceptance criteria on the bug ticket,
* be recorded as file impact and put through plan review before Phase 2
  continues (which is what escalates the bug out of the light advisory tier),
* stay portable across trackers.

These assert the *directive is present and coherent*, not its wording: each
check is a semantic requirement (a named command, a cross-reference, a
non-duplication bound), so a rewording that keeps the directive keeps the test
green.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).parents[2]
_SKILL = _REPO / "examples/agent-skills/rebar-debug/SKILL.md"
_GATE_DOC = _REPO / "docs/plan-review-gate.md"


def _section(text: str, start: str, end: str) -> str:
    """The lines from the heading beginning ``start`` up to the one beginning ``end``."""
    lines = text.splitlines()
    begin = next(i for i, line in enumerate(lines) if line.startswith(start))
    stop = next(i for i, line in enumerate(lines[begin + 1 :], begin + 1) if line.startswith(end))
    return "\n".join(lines[begin:stop])


@pytest.fixture(scope="module")
def step7() -> str:
    return _section(_SKILL.read_text(), "## Step 7 — Sweep for siblings", "## Reporting back")


@pytest.fixture(scope="module")
def r4() -> str:
    return _section(_GATE_DOC.read_text(), "## R4 — the necessity probe", "## The CI rigor signal")


def test_step7_routes_the_sibling_set_through_plan_review(step7: str) -> None:
    """The confirmed class is put through plan review, not just fixed site by site."""
    assert "review-plan" in step7


def test_step7_declares_the_sibling_set_as_file_impact(step7: str) -> None:
    """Declaring the sibling paths is what escalates the bug to the full rubric."""
    assert "set-file-impact" in step7


def test_step7_turns_each_sibling_into_an_acceptance_criterion(step7: str) -> None:
    assert "acceptance criteri" in step7.lower()


def test_step7_cross_references_the_phase_1_exit_gate(step7: str) -> None:
    """Step 7 links to the Phase-1 directive rather than restating it."""
    assert "phase 1" in step7.lower()


def test_step7_does_not_duplicate_the_blast_radius_rationale(step7: str) -> None:
    """A second copy of the escalation explanation would drift from the Phase-1 one."""
    assert step7.lower().count("blast radius") <= 1


def test_step7_stays_tracker_portable(step7: str) -> None:
    """The rebar command names must be conditioned on the tracker where they are given.

    Scoped to the block that introduces them, so an unrelated mention of
    "tracker" elsewhere in Step 7 cannot satisfy this.
    """
    items = [i for i in re.split(r"\n(?=\d+\. )", step7) if "set-file-impact" in i]
    assert items, "no step introduces the file-impact command"
    for item in items:
        # The rebar name is conditioned on the tracker, AND the same step says what to
        # do under any other one — twice, so dropping either half fails.
        assert item.lower().count("tracker") >= 2, item


def test_gate_doc_documents_the_blast_radius_escalation_under_r4(r4: str) -> None:
    assert "bug_blast_radius_escalates" in r4


@pytest.mark.parametrize("claim", ["coached but never BLOCKED", "never blocks a bug"])
def test_gate_doc_never_blocks_claims_name_the_escalation_exception(claim: str) -> None:
    """The shipped escalation contradicts an unqualified 'a bug can never block'.

    The claim must still be *stated* — the doc has to describe the tier's
    non-blocking default somewhere — and every statement of it must name the
    escalation. Requiring the claim to be present is what keeps this from
    passing vacuously if the passage is reworded away.
    """
    stating = [line for line in _GATE_DOC.read_text().splitlines() if claim in line]
    assert stating, f"the bug tier's non-blocking default is no longer stated as {claim!r}"
    for line in stating:
        assert "escalat" in line.lower(), (
            f"unqualified never-blocks claim contradicts the escalation: {line!r}"
        )
