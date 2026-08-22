"""Internal-consistency gate for the rebar-janitor discovery concern list.

``examples/agent-skills/rebar-janitor/phases/discovery.md`` states its concerns
twice: once as a top-level numbered list, and once as a spelled-out count in the
output schema's ``concern`` field ("which of the N concerns above"). Nothing
otherwise ties the two together: a concern can be added while the schema keeps
the old count, handing every discovery subagent a schema that contradicts the
list it is told to classify against.

The checks below DERIVE both numbers from the document rather than pinning
either, so they survive any rewording or reordering of the concerns themselves
and fail only on a genuine disagreement.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY = REPO_ROOT / "examples" / "agent-skills" / "rebar-janitor" / "phases" / "discovery.md"

#: Spelled-out cardinals the schema sentence may legitimately use. Deliberately
#: covers a generous range so adding a concern needs no edit here until the list
#: grows past twenty.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

_CONCERN_RE = re.compile(r"^(\d+)\. \*\*", re.MULTILINE)
_SCHEMA_RE = re.compile(r"which of the ([a-z]+) concerns above")


def _text() -> str:
    return DISCOVERY.read_text(encoding="utf-8")


def _numbered_concerns(text: str) -> list[int]:
    return [int(n) for n in _CONCERN_RE.findall(text)]


def _schema_count(text: str) -> int:
    matches = _SCHEMA_RE.findall(text)
    assert matches, (
        f"{DISCOVERY} has no 'which of the <N> concerns above' schema sentence; "
        "the output schema no longer states how many concerns it classifies against."
    )
    assert len(set(matches)) == 1, (
        f"{DISCOVERY} states the concern count more than once and the statements "
        f"disagree: {sorted(set(matches))}."
    )
    word = matches[0]
    assert word in NUMBER_WORDS, (
        f"{DISCOVERY} spells the concern count as {word!r}, which this gate cannot "
        f"read. Add it to NUMBER_WORDS (known: {sorted(NUMBER_WORDS)})."
    )
    return NUMBER_WORDS[word]


def test_discovery_defines_a_numbered_concern_list() -> None:
    """The document must actually carry a numbered concern list to check."""
    assert _numbered_concerns(_text()), (
        f"{DISCOVERY} exposes no top-level numbered concerns; the parser below "
        "would then compare the schema count against nothing and pass vacuously."
    )


def test_concern_numbers_are_contiguous_from_one() -> None:
    """A duplicated or skipped ordinal silently mis-sizes the list."""
    concerns = _numbered_concerns(_text())
    assert concerns == list(range(1, len(concerns) + 1)), (
        f"{DISCOVERY} numbers its concerns {concerns}; expected a contiguous run "
        f"1..{len(concerns)}. A repeated or skipped number makes the count the "
        "output schema quotes wrong even when it matches the item tally."
    )


def test_schema_concern_count_matches_the_concern_list() -> None:
    """The schema's spelled-out count must equal the number of concerns."""
    text = _text()
    concerns = _numbered_concerns(text)
    stated = _schema_count(text)
    highest = concerns[-1] if concerns else None
    assert stated == len(concerns), (
        f"{DISCOVERY} lists {len(concerns)} numbered concerns "
        f"(highest: {highest}) but its output schema says the subagent must "
        f"pick 'which of the {stated} concerns above'. Adding or removing a "
        "concern requires updating that sentence in the same change."
    )
