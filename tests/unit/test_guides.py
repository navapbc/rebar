"""Ticket 34c9: the plan-writing guide documents the Pass-4 coaching moves.

The guide's "Responding to coaching moves" section names the top-five field-frequency moves
with a worked before/after example each, documents the `[operator-attested]` AC tag under the
"state attestation evidence" entry, and indexes every remaining registered move — with every
name matching the `MOVE_REGISTRY` spelling exactly (read from the packaged `rebar._guides`
resource, so an installed rebar serves it from any working directory).
"""

from __future__ import annotations

from importlib import resources

import pytest

from rebar.llm.plan_review.coach_moves import MOVE_REGISTRY

pytestmark = pytest.mark.unit

SECTION = "## Responding to coaching moves"

# Top five moves by field frequency (spec-by-example 150, plan-the-verification 96,
# riskiest-assumption 51, state-attestation 41, propagate-to-children 22) — spelled
# exactly as MOVE_REGISTRY registers them.
TOP_FIVE = [
    "specification by example",
    "plan the verification",
    "riskiest-assumption test",
    "state attestation evidence",
    "propagate to children",
]


def _guide_text() -> str:
    base = resources.files("rebar._guides")
    return (base / "writing-a-passing-plan.md").read_text(encoding="utf-8")


def _section_text() -> str:
    text = _guide_text()
    assert SECTION in text, "guide is missing the coaching-moves section"
    body = text.split(SECTION, 1)[1]
    # Section runs until the next same-level (##) heading, if any.
    for line in body.splitlines():
        if line.startswith("## ") and not line.startswith("###"):
            body = body.split("\n" + line, 1)[0]
            break
    return body


def test_top_five_names_match_registry_spelling() -> None:
    registered = {m["name"] for m in MOVE_REGISTRY.values()}
    for name in TOP_FIVE:
        assert name in registered, f"test fixture out of sync with MOVE_REGISTRY: {name!r}"


def test_section_names_top_five_in_frequency_order_with_examples() -> None:
    body = _section_text()
    positions = []
    for name in TOP_FIVE:
        heading = f"### {name}"
        assert heading in body, f"missing entry for move {name!r}"
        positions.append(body.index(heading))
    assert positions == sorted(positions), "moves are not in field-frequency order"
    # Every top-five entry carries a before/after example.
    for name, start in zip(TOP_FIVE, positions, strict=True):
        end = min((p for p in positions if p > start), default=len(body))
        entry = body[start:end]
        assert "Before" in entry and "After" in entry, f"move {name!r} lacks a before/after example"


def test_operator_attested_tag_documented() -> None:
    body = _section_text()
    start = body.index("### state attestation evidence")
    end = body.index("### propagate to children")
    assert "[operator-attested]" in body[start:end], (
        "the [operator-attested] AC tag is not documented in the state-attestation entry"
    )


def test_remaining_registered_moves_indexed() -> None:
    body = _section_text()
    remaining = [m["name"] for m in MOVE_REGISTRY.values() if m["name"] not in TOP_FIVE]
    assert remaining, "registry unexpectedly has no non-top-five moves"
    for name in remaining:
        assert name in body, f"remaining move {name!r} missing from the one-line index"


def test_advisory_section_cross_links_to_moves() -> None:
    text = _guide_text()
    advisory = text.split("## Advisories worth heeding", 1)[1]
    advisory = advisory.split("\n## ", 1)[0]
    assert "#responding-to-coaching-moves" in advisory, (
        "advisory section does not cross-link the coaching-moves section"
    )
