"""Contract between the event-schema relation inventory and its cited source of truth.

The relation vocabulary has three sides that must agree: the ``Relation`` Literal in
``rebar.types``, the ``relation`` enum in ``common.schema.json``, and the hand-copied
``CANONICAL_RELATIONS`` frozenset that actually gates link creation at
``graph/_links.py``. This module pins all three to each other AND to ``docs/event-schema.md``,
so no pair can drift while another agrees (mirror F8).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from rebar import schemas
from rebar.graph._links import CANONICAL_RELATIONS
from rebar.types import Relation

_EVENT_SCHEMA = Path(__file__).resolve().parents[3] / "docs" / "event-schema.md"

#: How each side is named in a drift message, so a failure says which one to edit.
_GATE = "graph/_links.py:CANONICAL_RELATIONS"
_LITERAL = "rebar.types.Relation"
_SCHEMA = "common.schema.json#/$defs/relation"


def _link_event_row() -> str:
    text = _EVENT_SCHEMA.read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith("| `LINK` / `UNLINK` |"))


def _schema_relations() -> set[str]:
    """The relation enum as the canonical schema declares it."""
    return set(schemas.load("common")["$defs"]["relation"]["enum"])


def _relation_parity_failure(
    gate: set[str] | frozenset[str],
    literal: set[str] | frozenset[str],
    schema: set[str] | frozenset[str],
) -> str | None:
    """Describe how the three relation vocabularies disagree, or ``None`` if they agree.

    Pure, so the divergence behaviour this module claims is itself testable: the parity
    test feeds it the real three sides, and the mutation tests feed it a perturbed one.
    """
    sides = ((_GATE, set(gate)), (_LITERAL, set(literal)), (_SCHEMA, set(schema)))
    union: set[str] = set().union(*(values for _, values in sides))
    complaints = [
        f"{name} is missing {sorted(union - values)}" for name, values in sides if union - values
    ]
    if not complaints:
        return None
    return "relation vocabulary drifted: " + "; ".join(complaints)


def test_relation_vocabulary_is_pinned_three_ways() -> None:
    """AC1: the gate, the Literal and the schema declare the same relations."""
    failure = _relation_parity_failure(
        CANONICAL_RELATIONS, set(get_args(Relation)), _schema_relations()
    )
    assert failure is None, failure


# The divergence tests perturb ONE side of an agreeing baseline. The baseline is read from
# the schema rather than re-listed, so it cannot go stale — but it is used for all three
# sides, so these stay self-consistent and a REAL drift fails only the parity test above,
# instead of cascading five failures over one root cause.
def _agreeing_sides() -> tuple[set[str], set[str], set[str]]:
    baseline = _schema_relations()
    return set(baseline), set(baseline), set(baseline)


def test_parity_failure_names_the_gate_when_the_gate_drops_a_relation() -> None:
    """AC2: dropping a relation from CANONICAL_RELATIONS is reported against the gate."""
    gate, literal, schema = _agreeing_sides()
    failure = _relation_parity_failure(gate - {"caused_by"}, literal, schema)
    assert failure is not None
    assert _GATE in failure and "caused_by" in failure
    assert _LITERAL not in failure and _SCHEMA not in failure


def test_parity_failure_names_the_literal_when_the_literal_drops_a_relation() -> None:
    """AC2: dropping a relation from types.Relation is reported against the Literal."""
    gate, literal, schema = _agreeing_sides()
    failure = _relation_parity_failure(gate, literal - {"supersedes"}, schema)
    assert failure is not None
    assert _LITERAL in failure and "supersedes" in failure
    assert _GATE not in failure and _SCHEMA not in failure


def test_parity_failure_names_the_schema_when_the_schema_drops_a_relation() -> None:
    """AC2: dropping a relation from the schema enum is reported against the schema."""
    gate, literal, schema = _agreeing_sides()
    failure = _relation_parity_failure(gate, literal, schema - {"blocks"})
    assert failure is not None
    assert _SCHEMA in failure and "blocks" in failure
    assert _GATE not in failure and _LITERAL not in failure


def test_parity_failure_reports_a_relation_only_one_side_added() -> None:
    """AC2: an addition is drift too — the two sides that lack it are both named."""
    gate, literal, schema = _agreeing_sides()
    failure = _relation_parity_failure(gate | {"mirrors"}, literal, schema)
    assert failure is not None
    assert "mirrors" in failure
    assert _LITERAL in failure and _SCHEMA in failure
    assert _GATE not in failure


def test_divergence_tests_start_from_an_agreeing_baseline() -> None:
    """The perturbation fixture must itself be drift-free, or the AC2 tests prove nothing."""
    assert _relation_parity_failure(*_agreeing_sides()) is None


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
