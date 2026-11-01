"""Doc prose that enumerates a code vocabulary is pinned to that vocabulary (mirror F10).

Two enumerations across two documents are pinned here. Neither is generated or gated:
``gen_api_surface.py`` pins library SYMBOL NAMES only and never reads doc prose.

The census registers ARMS, not files, because the two do not share a parse shape — a markdown
table and a pipe-joined span. Each arm names WHERE the prose lives and WHICH master it answers
to; the expected values are always read from the master at assert time, never restated here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import get_args

import pytest

from rebar import schemas
from rebar.types import TicketStatus

_DOCS = Path(__file__).resolve().parents[3] / "docs"


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
        "status list",
        "user-guide.md",
        lambda text: _piped_span(text, "Statuses are"),
        lambda: set(get_args(TicketStatus)),
    ),
]


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
    """AC1: each enumerated list in prose equals the vocabulary it describes."""
    drift = list_arm_drift(label, doc, extract(_read(doc)), master())
    assert drift is None, drift
