"""One TicketType partition defines plan-review exemptions. These tests prevent claim, create,
drift, and review consumers from maintaining competing type lists."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_args

import pytest

from rebar.types import PLAN_REVIEW_EXEMPT_TYPES, PLAN_REVIEW_REVIEWED_TYPES, TicketType

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The six sites that used to carry their own copy.
CONSUMER_MODULES = [
    "src/rebar/_commands/gates.py",
    "src/rebar/_commands/composer.py",
    "src/rebar/llm/plan_review/orchestrator.py",
    "src/rebar/llm/plan_review/claimability.py",
    "src/rebar/_commands/transition_close.py",
]


def test_the_two_sets_partition_ticket_type() -> None:
    """AC1. Disjoint, and their union is exactly TicketType — no member unaccounted for."""
    members = set(get_args(TicketType))
    assert PLAN_REVIEW_EXEMPT_TYPES & PLAN_REVIEW_REVIEWED_TYPES == set()
    assert PLAN_REVIEW_EXEMPT_TYPES | PLAN_REVIEW_REVIEWED_TYPES == members, (
        "a TicketType member is in neither the exempt nor the reviewed set, so the "
        "plan-review gate's behavior for it is undecided: "
        f"{sorted(members - PLAN_REVIEW_EXEMPT_TYPES - PLAN_REVIEW_REVIEWED_TYPES)}"
    )


def test_the_complement_is_derived_not_listed() -> None:
    """AC1. If the reviewed set were re-typed it could disagree with the exemption; being a
    derived difference makes that unrepresentable."""
    assert PLAN_REVIEW_REVIEWED_TYPES == set(get_args(TicketType)) - PLAN_REVIEW_EXEMPT_TYPES


def test_an_eighth_ticket_type_would_be_unaccounted_for() -> None:
    """AC3. The failure mode this ticket exists to prevent, made explicit: adding a member to
    TicketType without deciding its side leaves it in neither set. Simulated rather than
    actually extending the Literal, so the test states the invariant without mutating types.
    """
    extended = set(get_args(TicketType)) | {"proposal"}
    unaccounted = extended - PLAN_REVIEW_EXEMPT_TYPES - PLAN_REVIEW_REVIEWED_TYPES
    assert unaccounted == {"proposal"}, (
        "a new TicketType member must be unaccounted-for until it is placed deliberately; "
        "if this is empty the sets are defaulting a type instead of forcing the decision"
    )


def test_behavior_is_unchanged_for_every_current_type() -> None:
    """AC5. The literals the six sites carried before this change, pinned so the
    consolidation cannot have quietly moved a type from one side to the other."""
    assert PLAN_REVIEW_EXEMPT_TYPES == {"bug", "session_log", "code_review", "identity"}
    assert PLAN_REVIEW_REVIEWED_TYPES == {"task", "story", "epic"}


@pytest.mark.parametrize("module_path", CONSUMER_MODULES)
def test_no_consumer_re_lists_the_ticket_type_literals(module_path: str) -> None:
    """Reject multi-member literal subsets of TicketType in consumers. They duplicate the shared
    exemption partition even when their order or exact members differ."""
    members = set(get_args(TicketType))
    tree = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.Set | ast.List):
            continue
        values = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if len(values) != len(node.elts) or len(values) < 2:
            continue
        if set(values) < members:
            offenders.append(sorted(values))
    assert not offenders, (
        f"{module_path} re-lists ticket-type members instead of importing the canonical "
        f"sets from rebar.types: {offenders}"
    )


@pytest.mark.parametrize("module_path", CONSUMER_MODULES)
def test_every_consumer_imports_the_canonical_sets(module_path: str) -> None:
    """AC2, the positive half: the site references the shared declaration."""
    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    assert "PLAN_REVIEW_EXEMPT_TYPES" in source or "PLAN_REVIEW_REVIEWED_TYPES" in source


def test_the_canonical_sets_live_beside_ticket_type() -> None:
    """Keeping them in types.py is what makes the derivation possible without an import
    cycle — types.py imports only `typing`, so every consumer can reach it."""
    import rebar.types as types_module

    tree = ast.parse(inspect.getsource(types_module))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= {"typing", "__future__"}, (
        f"rebar.types gained a non-stdlib import ({sorted(imported)}); it must stay a leaf "
        "or the six consumers cannot all import the canonical sets"
    )
