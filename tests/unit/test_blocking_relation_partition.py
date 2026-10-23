"""Keep cycle-capable relation names in the canonical set.

A stale local complement can treat a non-blocking relation as blocking and
reject a valid link with CyclicDependencyError."""

from __future__ import annotations

import ast
import inspect
from typing import get_args

import pytest

from rebar.graph import _graph, _links
from rebar.graph._relations import _BLOCKING_RELATIONS
from rebar.types import Relation

pytestmark = pytest.mark.unit


def test_blocking_is_a_proper_subset_of_the_relation_vocabulary() -> None:
    assert _BLOCKING_RELATIONS < set(get_args(Relation))
    assert _BLOCKING_RELATIONS == {"blocks", "depends_on"}


@pytest.mark.parametrize("module", [_graph, _links])
def test_neither_site_re_lists_relation_names(module) -> None:
    """Find proper literal subsets of relation names regardless of tuple order.
    The full canonical vocabulary is outside this guard."""
    members = set(get_args(Relation))
    tree = ast.parse(inspect.getsource(module))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.Set | ast.List):
            continue
        values = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if len(values) == len(node.elts) and len(values) >= 2 and set(values) < members:
            offenders.append(sorted(values))
    assert not offenders, (
        f"{module.__name__} re-lists relation names instead of using _BLOCKING_RELATIONS: "
        f"{offenders}"
    )


def test_a_non_blocking_relation_is_never_cycle_checked() -> None:
    """AC2. The regression this prevents: anything outside _BLOCKING_RELATIONS must short
    -circuit to False before any graph walk, including a relation added later."""
    for relation in set(get_args(Relation)) - _BLOCKING_RELATIONS:
        assert (
            _graph.check_would_create_cycle("a", "b", relation, "/nonexistent-tracker") is False
        ), f"{relation!r} was cycle-checked; it is not a blocking relation"


def test_a_hypothetical_new_non_blocking_relation_short_circuits() -> None:
    """AC2. A name that is not in _BLOCKING_RELATIONS must return False without touching the
    tracker dir — proven by passing a path that does not exist."""
    assert _graph.check_would_create_cycle("a", "b", "endorses", "/nonexistent-tracker") is False


@pytest.mark.parametrize("module", [_graph, _links])
def test_the_prose_no_longer_enumerates_the_non_blocking_relations(module) -> None:
    """AC4. The drift was in the COMMENTS, so the fix has to reach them: an enumeration that
    must be maintained by hand is what went stale."""
    source = inspect.getsource(module)
    assert "'relates_to', 'duplicates', 'supersedes', and 'discovered_from'" not in source
    assert "relates_to / duplicates / supersedes / discovered_from" not in source
