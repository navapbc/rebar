"""One criteria heading, not two (ticket finicky-rainbowy-weevil).

`## Success Criteria` and `## Acceptance Criteria` were two headings for one concept.
Only three sites READ the SC heading to make a decision: the epic branch of
`_clarity_score` (in `_engine_support/gates.py` and its deliberate lockstep copy in
`llm/plan_review/det_floor.py`), and `_SCOPE_HEADINGS` in `llm/code_review/assemble.py`.

Pinned here, as observable behaviour:

* an epic whose criteria live under `## Acceptance Criteria` earns the epic clarity
  bonus (score 6) and passes both `clarity-check` and `check-ac`;
* `## Success Criteria` is inert — adding it earns no bonus and changes no score;
* the two `_clarity_score` copies stay in lockstep, as their module comments assert;
* the code-review scope extractor no longer surfaces an SC section.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar._engine_support.gates import _clarity_score, clarity_check_compute
from rebar.llm.code_review.assemble import _scope_sections
from rebar.llm.plan_review.det_floor import _clarity_score as _det_clarity_score

_FILLER = (
    "This ticket describes a cohesive unit of work in enough detail for an "
    "agent to act on it without guessing. " * 3
)
_CRITERIA_BULLETS = "- [ ] first criterion\n- [ ] second criterion\n"

# The two remaining epic clarity signals: a criteria block plus ## Context.
EPIC_AC = _FILLER + "\n\n## Context\nWhy now.\n\n## Acceptance Criteria\n" + _CRITERIA_BULLETS
# Byte-for-byte the same ticket with the criteria filed under the retired heading.
EPIC_SC = EPIC_AC.replace("## Acceptance Criteria", "## Success Criteria")


def _cli_rc(*args: str, cwd: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    ).returncode


# ── happy path ────────────────────────────────────────────────────────────────
def test_epic_with_acceptance_criteria_scores_six_and_passes_both_gates(
    rebar_repo: Path,
) -> None:
    """An epic filing its criteria under ## Acceptance Criteria earns the epic bonus.

    6 is the score the SC-headed fixture earned before the collapse, so this pins the
    repoint as score-neutral: no length padding is needed to clear the threshold of 5.
    """
    assert _clarity_score(EPIC_AC, "epic") == 6
    assert clarity_check_compute("epic", EPIC_AC, 5)[1] == 0

    r = str(rebar_repo)
    tid = rebar.create_ticket("epic", "epic ac probe", description=EPIC_AC, repo_root=r)
    assert _cli_rc("clarity-check", tid, cwd=r) == 0, "well-formed AC epic failed clarity-check"
    assert _cli_rc("check-ac", tid, cwd=r) == 0, "well-formed AC epic failed check-ac"


# ── held-out: the SC heading is inert ─────────────────────────────────────────
def test_success_criteria_heading_earns_no_epic_bonus() -> None:
    """The retired heading no longer contributes the +2 epic bonus."""
    assert _clarity_score(EPIC_SC, "epic") == _clarity_score(EPIC_AC, "epic") - 2


def test_success_criteria_block_is_inert_alongside_acceptance_criteria(rebar_repo: Path) -> None:
    """An extra SC block changes nothing: the epic already scored its bonus from AC.

    Isolates the repoint. An SC-*only* epic cannot demonstrate it — such a ticket fails
    clarity anyway on the universal AC floor, whichever heading the bonus keys on.
    """
    both = EPIC_AC + "\n## Success Criteria\n- retired prose\n"
    # Guard the arithmetic: neither variant may cross the >=500-char length bonus, or the
    # comparison below would be measuring length rather than the heading.
    assert len(EPIC_AC) < 500 and len(both) < 500

    assert _clarity_score(both, "epic") == _clarity_score(EPIC_AC, "epic")

    r = str(rebar_repo)
    tid = rebar.create_ticket("epic", "epic both", description=both, repo_root=r)
    assert _cli_rc("clarity-check", tid, cwd=r) == 0
    assert _cli_rc("check-ac", tid, cwd=r) == 0


# ── held-out: the two heuristic copies stay in lockstep ───────────────────────
@pytest.mark.parametrize(
    ("ticket_type", "description"),
    [
        ("epic", EPIC_AC),
        ("epic", EPIC_SC),
        ("task", _FILLER + "\n\n## Acceptance Criteria\n" + _CRITERIA_BULLETS),
        ("story", _FILLER + "\n\n## Why\nw\n\n## What\nx\n\n## Scope\ns\n"),
    ],
)
def test_det_floor_clarity_copy_stays_in_lockstep(ticket_type: str, description: str) -> None:
    """det_floor's copy of the heuristic must score identically to the gate's.

    Restricted to the per-type branches both copies implement (task/story/epic): the
    det_floor copy has never carried a `bug` branch, a pre-existing divergence this
    story does not touch.
    """
    assert _det_clarity_score(description, ticket_type) == _clarity_score(description, ticket_type)


# ── held-out: non-epic branches are untouched ─────────────────────────────────
@pytest.mark.parametrize(
    ("ticket_type", "description", "expected"),
    [
        # heading +1, len>=200 +1, bullet +1, AC +2, path-like token +1
        ("task", _FILLER + "\n\n## Acceptance Criteria\n- [ ] touch src/rebar/foo.py\n", 6),
        # heading +1, len>=200 +1, bullet +1, Why+What +2, Scope +1
        ("story", _FILLER + "\n\n## Why\nw\n\n## What\nx\n\n## Scope\n- s\n", 6),
        # heading +1, len>=200 +1, bullet +1, Reproduction Steps +2, expected/actual +1
        ("bug", _FILLER + "\n\n## Reproduction Steps\n- run it\n\nExpected X, actual Y.\n", 6),
    ],
)
def test_non_epic_scores_are_unchanged(ticket_type: str, description: str, expected: int) -> None:
    assert _clarity_score(description, ticket_type) == expected


# ── held-out: the code-review scope extractor drops SC ────────────────────────
def test_scope_sections_drops_a_success_criteria_section() -> None:
    """`_SCOPE_HEADINGS` no longer surfaces SC prose into the code-review scope context."""
    description = (
        "## What\nthe change itself\n\n"
        "## Success Criteria\n- retired sc prose\n\n"
        "## Acceptance Criteria\n- [ ] live ac prose\n\n"
        "## Notes\nnot a scope heading\n"
    )
    out = _scope_sections(description)

    assert "the change itself" in out
    assert "live ac prose" in out
    assert "retired sc prose" not in out, "SC section still surfaced into code-review scope"
    assert "## Success Criteria" not in out
    assert "not a scope heading" not in out


def test_scope_sections_still_falls_back_when_no_scope_heading_present() -> None:
    """Removing SC must not change the whole-description fallback for headingless bodies."""
    description = "Just a paragraph with no markdown headings at all.\n"
    assert _scope_sections(description) == description.strip()
