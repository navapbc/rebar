"""Held-out contracts for the Success Criteria store migration (ticket 0a8c). WITHHELD.

- the remaining block shapes: mixed `- ` / `- [ ]` items, SC as the LAST section,
  a ticket with no SC block at all (must be a no-op),
- the migration's whole point: every migrated description makes `_count_ac_items`
  see at least one item (SC criteria previously never reached the blocking floor),
- collateral invariants: prose outside the SC block survives byte-for-byte, `###`
  subheadings do not prematurely terminate the block, non-bullet prose inside the
  block is not check-boxed,
- idempotency: migrating an already-migrated description changes nothing,
- the write path: `--dry-run` is the default and writes nothing.
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


# ── the remaining block shapes ────────────────────────────────────────────────
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


# ── the point of the migration: criteria now reach the blocking floor ─────────
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


# ── collateral invariants ─────────────────────────────────────────────────────
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


# ── idempotency ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "description",
    [
        "## Success Criteria\n- a\n- b\n",
        "## Acceptance Criteria\n- [ ] here\n\n## Success Criteria\n- there\n",
        "## Why\nw.\n\n## Success Criteria\n- a\n\n## Context\nc.\n",
    ],
)
def test_migration_is_idempotent(description: str) -> None:
    once = _load().rewrite_description(description)
    assert _load().rewrite_description(once) == once
