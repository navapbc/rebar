"""Doc prose that enumerates a code vocabulary is pinned to that vocabulary (mirror F10).

Five enumerations across three documents had drifted from their masters. None was generated
or gated: ``gen_api_surface.py`` pins library SYMBOL NAMES only and never reads doc prose.

The census registers ARMS, not files, because the five do not share a parse shape — a markdown
table, pipe-joined spans with and without spaces, and one that is not a list at all but the
English word for a count. Each arm names WHERE the prose lives and WHICH master it answers to;
the expected values are always read from the master at assert time, never restated here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import pytest

from rebar import schemas
from rebar.types import Relation, TicketStatus, TicketType

_DOCS = Path(__file__).resolve().parents[3] / "docs"

#: Number words this census can read, up to the largest vocabulary it pins.
_NUMBER_WORDS = {
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _schema_verdicts() -> set[str]:
    """The verify-signature verdicts as the canonical schema declares them."""
    schema = schemas.load("verify_signature_result")

    def _find(node: object) -> list[str] | None:
        if isinstance(node, dict):
            enum = node.get("enum")
            if isinstance(enum, list) and "certified" in enum:
                return [str(value) for value in enum]
            for value in node.values():
                found = _find(value)
                if found:
                    return found
        return None

    verdicts = _find(schema)
    assert verdicts, "verify_signature_result.schema.json no longer declares a verdict enum"
    return set(verdicts)


def _table_column(text: str, heading: str) -> set[str] | None:
    """Backticked values in the first column of the markdown table under ``heading``."""
    start = text.find(f"| {heading} |")
    if start == -1:
        return None
    values: set[str] = set()
    for line in text[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cell = line.split("|")[1].strip()
        match = re.fullmatch(r"`([a-z_]+)`", cell)
        if match:
            values.add(match.group(1))
    return values or None


def _piped_span(text: str, prefix: str) -> set[str] | None:
    """Values in the single backticked ``a|b|c`` span that follows ``prefix``."""
    match = re.search(re.escape(prefix) + r"\s*\n?`([^`]+)`", text)
    if match is None:
        return None
    return {part.strip() for part in match.group(1).split("|") if part.strip()}


def _count_word(text: str, pattern: str) -> int | None:
    """The integer named by the number word ``pattern`` captures."""
    match = re.search(pattern, text)
    if match is None:
        return None
    return _NUMBER_WORDS.get(match.group(1).lower())


def _read(name: str) -> str:
    return (_DOCS / name).read_text(encoding="utf-8")


#: (arm label, doc, extractor, master) — the master is read at assert time, never copied.
_LIST_ARMS: list[tuple[str, str, Callable[[str], set[str] | None], Callable[[], set[str]]]] = [
    (
        "verdict table",
        "reuse-surface.md",
        lambda text: _table_column(text, "Verdict"),
        _schema_verdicts,
    ),
    (
        "native-model types",
        "oss-comparison-and-remediation.md",
        lambda text: _piped_span(text, "types:"),
        lambda: set(get_args(TicketType)),
    ),
    (
        "native-model statuses",
        "oss-comparison-and-remediation.md",
        lambda text: _piped_span(text, "statuses:"),
        lambda: set(get_args(TicketStatus)),
    ),
    (
        "status list",
        "user-guide.md",
        lambda text: _piped_span(text, "Statuses are"),
        lambda: set(get_args(TicketStatus)),
    ),
]

#: The fifth arm is a COUNT in prose ("six relations"), not a list.
_COUNT_ARM = (
    "relation count",
    "oss-comparison-and-remediation.md",
    r"\b(three|four|five|six|seven|eight|nine|ten)\s+\n?relations\b",
    lambda: len(get_args(Relation)),
)


def list_arm_drift(label: str, doc: str, found: set[str] | None, expected: set[str]) -> str | None:
    """How a list arm differs from its master, or ``None`` if it agrees.

    ``found is None`` means the passage could not be located — reported as such rather than as
    an empty set, so a reworded document blames the parser instead of every value in the master.
    """
    if found is None:
        return (
            f"{doc} [{label}]: could not locate the enumeration, so it is no longer gated. "
            "Restore the passage or update its extractor in this census."
        )
    if found == expected:
        return None
    return (
        f"{doc} [{label}] drifted from its code master: "
        f"missing {sorted(expected - found)}, stale/extra {sorted(found - expected)}."
    )


@pytest.mark.parametrize(("label", "doc", "extract", "master"), _LIST_ARMS)
def test_list_arm_matches_its_master(
    label: str,
    doc: str,
    extract: Callable[[str], set[str] | None],
    master: Callable[[], set[str]],
) -> None:
    """AC1-AC3: each enumerated list in prose equals the vocabulary it describes."""
    drift = list_arm_drift(label, doc, extract(_read(doc)), master())
    assert drift is None, drift


def test_relation_count_matches_its_master() -> None:
    """AC2: the fifth arm is a number word, so it is checked as a count."""
    label, doc, pattern, master = _COUNT_ARM
    found = _count_word(_read(doc), pattern)
    assert found is not None, f"{doc} [{label}]: could not locate the relation count"
    assert found == master(), (
        f"{doc} [{label}] says {found} relations; rebar.types.Relation declares {master()}."
    )
