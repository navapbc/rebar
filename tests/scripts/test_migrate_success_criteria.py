"""Happy-path contract for the Success Criteria store migration (ticket 0a8c).

Tier: unit (pure text rewrite — no store, no git). Pins the core: a ticket carrying
a `## Success Criteria` block has its criteria moved under `## Acceptance Criteria`
as `- [ ]` checklist items, so they finally reach the check-ac blocking floor.

The script exposes `rewrite_description(description) -> str`, returning the migrated
description and returning the input UNCHANGED when there is no SC block.

Block shapes beyond the two below, collateral invariants, idempotency and the
write path are held out.

Held-out oracles merged from test_migrate_success_criteria_heldout: Held-out contracts for the
Success Criteria store migration (ticket 0a8c). WITHHELD.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rebar.llm.plan_review.det_floor import _count_ac_items

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_success_criteria.py"


def _load():
    spec = importlib.util.spec_from_file_location("migrate_success_criteria", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_success_criteria_only_becomes_an_acceptance_criteria_checklist() -> None:
    """SC is the ticket's only criteria block: rename it and check-box its bullets."""
    out = _load().rewrite_description(
        "## Why\nbecause.\n\n## Success Criteria\n- the thing ships\n- the other thing ships\n"
    )

    assert "## Success Criteria" not in out
    assert "## Acceptance Criteria" in out
    assert "- [ ] the thing ships" in out
    assert "- [ ] the other thing ships" in out
    assert "## Why\nbecause." in out


def test_success_criteria_merges_into_an_existing_acceptance_criteria_block() -> None:
    """Both headings present: SC's items join the existing AC list, SC heading goes."""
    out = _load().rewrite_description(
        "## Acceptance Criteria\n- [ ] already here\n\n## Success Criteria\n- the thing ships\n"
    )

    assert "## Success Criteria" not in out
    assert out.count("## Acceptance Criteria") == 1
    assert "- [ ] already here" in out
    assert "- [ ] the thing ships" in out


# ── held-out oracles (merged from test_migrate_success_criteria_heldout) ──


def test_mixed_bullet_styles_all_become_single_checkboxes() -> None:
    """An SC block already part-checkboxed must not end up with doubled markers."""
    out = _load().rewrite_description(
        "## Success Criteria\n- plain bullet\n- [ ] already a checkbox\n- [x] a ticked one\n"
    )

    assert "- [ ] plain bullet" in out
    assert "- [ ] already a checkbox" in out
    assert "- [ ] [ ]" not in out, "double checkbox marker"
    assert "- [ ] [x]" not in out
    # A ticked item stays ticked — migration moves criteria, it does not un-complete them.
    assert "- [x] a ticked one" in out


def test_success_criteria_as_the_last_section_is_migrated() -> None:
    """No trailing `## ` heading to bound the block: it runs to end of text."""
    out = _load().rewrite_description("## Why\nbecause.\n\n## Success Criteria\n- the last one\n")

    assert "## Success Criteria" not in out
    assert "- [ ] the last one" in out


def test_description_without_success_criteria_is_a_no_op() -> None:
    description = "## Why\nbecause.\n\n## Acceptance Criteria\n- [ ] untouched\n"
    assert _load().rewrite_description(description) == description


@pytest.mark.parametrize(
    "description",
    [
        "## Success Criteria\n- the thing ships\n",
        "## Why\nw.\n\n## Success Criteria\n- a\n- b\n",
        "## Acceptance Criteria\n- [ ] here\n\n## Success Criteria\n- there\n",
        "## Success Criteria\n- plain\n- [ ] boxed\n\n## Context\nc.\n",
    ],
)
def test_migrated_descriptions_reach_the_acceptance_floor(description: str) -> None:
    """Before migration these score 0 countable AC items; after, at least one."""
    assert _count_ac_items(_load().rewrite_description(description)) >= 1


def test_unmigrated_success_criteria_really_did_score_zero() -> None:
    """Guards the premise: SC bullets were invisible to the floor to begin with.

    Without this the test above could pass on a description that already counted.
    """
    assert _count_ac_items("## Success Criteria\n- the thing ships\n") == 0


def test_content_outside_the_block_survives_untouched() -> None:
    head = "## Why\nA paragraph that must not be reflowed.\n\n## Scope\n- a scope bullet\n"
    tail = "\n## Context\nTrailing prose.\n"
    out = _load().rewrite_description(head + "\n## Success Criteria\n- crit\n" + tail)

    assert head in out, "prose before the SC block was altered"
    assert tail in out, "prose after the SC block was altered"
    assert "- [ ] a scope bullet" not in out, "a bullet OUTSIDE the block was check-boxed"


def test_a_subheading_does_not_terminate_the_block() -> None:
    """`### Sub` is not a `## ` boundary — items after it are still SC criteria."""
    out = _load().rewrite_description(
        "## Success Criteria\n- before\n\n### Sub\n- after\n\n## Context\n- not a criterion\n"
    )

    assert "- [ ] before" in out
    assert "- [ ] after" in out
    assert "- [ ] not a criterion" not in out, "migration ran past the `## ` boundary"


def test_non_bullet_prose_inside_the_block_is_not_checkboxed() -> None:
    out = _load().rewrite_description(
        "## Success Criteria\nSome framing prose.\n\n- a real criterion\n"
    )

    assert "- [ ] a real criterion" in out
    assert "Some framing prose." in out
    assert "- [ ] Some framing prose." not in out


def test_success_criteria_before_acceptance_criteria_merges_correctly() -> None:
    """SC ahead of AC in document order must merge exactly as SC-after-AC does.

    Found by dry-running against the real store: roughly half of the affected
    tickets put SC first, and an order-sensitive merge silently duplicated the AC
    block instead of removing the SC one.
    """
    out = _load().rewrite_description(
        "## Success Criteria\n- the thing ships\n\n## Acceptance Criteria\n- [ ] already here\n"
    )

    assert "## Success Criteria" not in out, "SC heading survived the merge"
    assert out.count("## Acceptance Criteria") == 1, "AC heading was duplicated"
    assert out.count("the thing ships") == 1, "criterion was duplicated"
    assert out.count("already here") == 1
    assert "- [ ] the thing ships" in out
    assert "- [ ] already here" in out


@pytest.mark.parametrize(
    "description",
    [
        "## Success Criteria\n- a\n\n## Acceptance Criteria\n- [ ] b\n",
        "## Why\nw.\n\n## Success Criteria\n- a\n\n## Acceptance Criteria\n- [ ] b\n\n## Ctx\nc.\n",
        "## Acceptance Criteria\n- [ ] b\n\n## Success Criteria\n- a\n",
    ],
)
def test_no_heading_is_ever_duplicated(description: str) -> None:
    """Whatever the order, migration must never emit two AC headings."""
    out = _load().rewrite_description(description)
    assert out.count("## Acceptance Criteria") == 1
    assert "## Success Criteria" not in out


def test_headings_stay_on_their_own_line() -> None:
    """A heading must never be concatenated onto the end of a preceding line."""
    out = _load().rewrite_description(
        "## Success Criteria\n- a\n\n## Acceptance Criteria\n- [ ] b\n"
    )
    for line in out.splitlines():
        if "## Acceptance Criteria" in line:
            assert line.strip() == "## Acceptance Criteria", f"heading merged into a line: {line!r}"


@pytest.mark.parametrize(
    "heading",
    [
        "## Success Criteria (target state)",
        "## Success Criteria (outcomes, not mechanisms)",
        "## Success criteria",
        "##  Success Criteria",
    ],
)
def test_qualified_and_variant_headings_are_migrated(heading: str) -> None:
    """An exact-string match misses real headings. Found in the live store: two
    tickets carry a parenthetical qualifier and were silently left behind."""
    out = _load().rewrite_description(f"## Why\nw.\n\n{heading}\n- the thing ships\n")

    assert "Success Criteria" not in out and "Success criteria" not in out
    assert "## Acceptance Criteria" in out
    assert "- [ ] the thing ships" in out
    # The qualifier is part of the heading, never demoted into a criterion.
    assert "- [ ] (target state)" not in out


def test_a_backticked_prose_mention_is_not_a_heading() -> None:
    """Guards the other direction: prose referring to the heading must be left alone.

    Most store tickets matching the string only *discuss* it; migrating those would
    corrupt their prose.
    """
    description = "## Why\nSome tickets carry `## Success Criteria` in prose.\n"
    assert _load().rewrite_description(description) == description


@pytest.mark.parametrize(
    "description",
    [
        "## Success Criteria\n- a\n- b\n",
        "## Acceptance Criteria\n- [ ] here\n\n## Success Criteria\n- there\n",
        "## Why\nw.\n\n## Success Criteria\n- a\n\n## Context\nc.\n",
        # SC ahead of AC — the order the order-sensitive merge got wrong.
        "## Success Criteria\n- there\n\n## Acceptance Criteria\n- [ ] here\n",
    ],
)
def test_migration_is_idempotent(description: str) -> None:
    once = _load().rewrite_description(description)
    assert _load().rewrite_description(once) == once
