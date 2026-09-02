"""The plan-review type exemption partitions TicketType, from ONE declaration (mirror F3).

Ticket e755-9371-7951-454a (contiguous-industrial-dugong).

Which ticket types the plan-review gate reviews had SIX masters — the start-work gate, the
create-time file-impact nudge, the drift-refresh candidate path and the close gate each
carried their own tuple — and nothing asserted they partition ``TicketType``. Three of the
six claimed to be single-sourced; one of those claims cited a code path that had moved.

The counts happened to work out (4 exempt + 3 reviewed = 7 members), so the defect was
latent rather than live: an EIGHTH type would land in neither set and be neither gated nor
exempted, silently deciding whether the claim gate runs at all.
"""

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
    """AC2. A consumer that re-lists the members is a seventh master in waiting.

    Looks for any tuple/set/frozenset literal of string constants that is a PROPER subset
    of TicketType covering more than one member — the shape of an exemption or complement
    copy — rather than grepping for the exact old spelling, so reordering cannot evade it.

    A literal listing ALL members is deliberately not flagged here. That is a different
    mirror (composer's `_TYPES` vs `TicketType`), it is a distinct concept — every valid
    type to create, not the gate's exemption — and it has its own ticket in this epic,
    `boiling-gold-pterosaurs` (F7). Folding it in would put this change beyond its plan.
    """
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
