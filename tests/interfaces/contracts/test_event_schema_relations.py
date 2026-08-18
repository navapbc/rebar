"""Contract between the event-schema relation inventory and its cited source of truth."""

from __future__ import annotations

import re
from pathlib import Path

from rebar.graph._links import CANONICAL_RELATIONS

_EVENT_SCHEMA = Path(__file__).resolve().parents[3] / "docs" / "event-schema.md"


def _link_event_row() -> str:
    text = _EVENT_SCHEMA.read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith("| `LINK` / `UNLINK` |"))


def test_link_event_documents_every_canonical_relation_and_caused_by_semantics() -> None:
    row = _link_event_row()
    inventory = row.split("Relations:", 1)[1].split("(`graph/_links.py:CANONICAL_RELATIONS`)", 1)[0]
    documented = set(re.findall(r"`([a-z_]+)`", inventory))

    assert documented == set(CANONICAL_RELATIONS), (
        f"documented LINK relations drifted: missing={sorted(CANONICAL_RELATIONS - documented)}, "
        f"extra={sorted(documented - CANONICAL_RELATIONS)}"
    )
    semantic_clauses = [
        clause
        for clause in re.split(r"(?<=[.!?])\s+", row)
        if "`caused_by`" in clause and "directional" in clause
    ]
    assert semantic_clauses, "caused_by lacks an explicit semantic description"
    semantics = " ".join(semantic_clauses)
    for property_name in ("directional", "non-blocking", "non-cycle-inducing"):
        assert property_name in semantics, f"caused_by semantics omit {property_name}"
