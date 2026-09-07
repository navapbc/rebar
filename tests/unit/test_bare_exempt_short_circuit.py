"""Tests for the derived plan-review bare-exempt short circuit.

``plan_review_precheck`` derives its set from ``PLAN_REVIEW_EXEMPT_TYPES`` instead of listing
ticket types again. Bugs are excluded from the short circuit because they take the light
advisory tier with the deterministic floor and ``necessity`` probe. Tests pin both membership
and precheck behavior so vocabulary changes require an explicit decision.
"""

from __future__ import annotations

import ast
import inspect
from typing import get_args

import pytest

from rebar.llm.plan_review import workflow_ops
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review ops
from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext
from rebar.types import (
    PLAN_REVIEW_BARE_EXEMPT_TYPES,
    PLAN_REVIEW_BUG_TIER_TYPES,
    PLAN_REVIEW_EXEMPT_TYPES,
    TicketType,
)

pytestmark = pytest.mark.unit

_TARGET = "BARE-1"

#: A well-formed plan, so nothing in the DET floor blocks and the only thing deciding the
#: outcome is the ticket type. Mirrors the fixture shape in ``test_bug_review_tier.py``.
_PLAN = (
    "## Reproduction Steps\n1. do X.\n2. observe Y.\n\n"
    "**Expected:** Z.\n**Actual:** not Z.\n\n"
    "## What\nFix the handler in `src/rebar/x.py`.\n\n"
    "## Acceptance Criteria\n- [ ] Y no longer happens, covered by a test.\n\n"
    "## Testing\nRun `pytest tests/unit/test_x.py -q`.\n"
)


def _state(ttype: str) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Some ticket",
        "description": _PLAN,
        "deps": [],
    }


def _precheck(monkeypatch, ttype: str) -> dict:
    """Run the real precheck op offline (no LLM) for a ticket of ``ttype``."""
    state = _state(ttype)
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])
    ctx = StepContext(
        run_id="r",
        step_id="precheck",
        kind="scripted",
        step={},
        inputs={"ticket_id": _TARGET},
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    return STEP_REGISTRY["plan_review_precheck"](ctx)


# ── AC2: BEHAVIOR is unchanged — the three short-circuit, a bug still takes the tier ─────────
@pytest.mark.parametrize("ttype", ["session_log", "code_review", "identity"])
def test_bare_exempt_types_short_circuit_to_an_exempt_pass(monkeypatch, ttype: str) -> None:
    """AC2. Exactly these three reach the bare exempt PASS: no LLM, ``runner == "exempt"``."""
    out = _precheck(monkeypatch, ttype)
    assert out["run_llm"] is False
    assert out["verdict"]["verdict"] == "PASS"
    assert out["verdict"]["runner"] == "exempt"


def test_a_bug_still_takes_the_light_advisory_tier_not_a_bare_exempt_pass(monkeypatch) -> None:
    """Keep bugs in the light advisory tier instead of the bare-exempt path.

    Precheck takes the LLM arm, emits no terminal verdict, and records bug-tier coverage.
    """
    out = _precheck(monkeypatch, "bug")
    assert out["run_llm"] is True
    assert out["verdict"] is None
    assert out["det_coverage"].get("bug_tier") is True


@pytest.mark.parametrize("ttype", ["task", "story", "epic"])
def test_reviewed_types_are_untouched_by_the_short_circuit(monkeypatch, ttype: str) -> None:
    """AC2. The reviewed types keep running the full review — the short-circuit did not widen."""
    out = _precheck(monkeypatch, ttype)
    assert out["run_llm"] is True
    assert out["verdict"] is None


# ── AC1: the module carries no ticket-type spelling of its own ───────────────────────────────
def test_workflow_ops_spells_no_ticket_type_literal() -> None:
    """AC1. Any bare string constant equal to a ``TicketType`` member is an unpinned spelling.

    Checked structurally over the AST rather than by grepping for the exact old tuple, so
    reordering, re-splitting or restating the list cannot evade it. Comments are not AST
    nodes, so the explanatory prose about bugs is unaffected.
    """
    members = set(get_args(TicketType))
    tree = ast.parse(inspect.getsource(workflow_ops))
    offenders = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in members
        }
    )
    assert not offenders, (
        "rebar.llm.plan_review.workflow_ops spells ticket types itself instead of importing "
        f"the derived sets from rebar.types: {offenders}"
    )


def test_workflow_ops_consumes_the_derived_sets() -> None:
    """AC1, the positive half: the module reaches the vocabulary through ``rebar.types``.

    Asserted on the resolved module attributes, not on source text, so moving the import or
    renaming the alias is fine as long as the same objects are in play.
    """
    assert workflow_ops.PLAN_REVIEW_BARE_EXEMPT_TYPES is PLAN_REVIEW_BARE_EXEMPT_TYPES
    assert workflow_ops.PLAN_REVIEW_BUG_TIER_TYPES is PLAN_REVIEW_BUG_TIER_TYPES


# ── AC3: every member is a real TicketType ───────────────────────────────────────────────────
def test_every_member_is_a_ticket_type_member() -> None:
    """AC3. The failure this ticket exists to prevent: a spelling that is not a real type.

    A renamed ``TicketType`` member used to leave the tuple's stale spelling matching
    nothing, silently turning the short-circuit off. It now fails here instead.
    """
    members = set(get_args(TicketType))
    assert PLAN_REVIEW_BARE_EXEMPT_TYPES <= members
    assert PLAN_REVIEW_BUG_TIER_TYPES <= members


# ── AC4: the derivation, and the relation to the claim-gate exemption ────────────────────────
def test_the_bare_exempt_set_is_derived_from_the_exemption() -> None:
    """AC4. Derived, never re-listed — the two sets cannot drift apart independently."""
    assert PLAN_REVIEW_BARE_EXEMPT_TYPES == PLAN_REVIEW_EXEMPT_TYPES - PLAN_REVIEW_BUG_TIER_TYPES


def test_the_two_tiers_partition_the_exempt_set() -> None:
    """AC4. Every gate-exempt type takes exactly one of the two tiers.

    ``PLAN_REVIEW_BUG_TIER_TYPES`` is not free-floating: a type given the light tier must
    also be exempt from the claim gate, or the subtraction silently removes nothing.
    """
    assert PLAN_REVIEW_BUG_TIER_TYPES <= PLAN_REVIEW_EXEMPT_TYPES
    assert PLAN_REVIEW_BARE_EXEMPT_TYPES & PLAN_REVIEW_BUG_TIER_TYPES == set()
    assert PLAN_REVIEW_BARE_EXEMPT_TYPES | PLAN_REVIEW_BUG_TIER_TYPES == PLAN_REVIEW_EXEMPT_TYPES


def test_membership_is_pinned_so_a_new_exempt_type_forces_a_decision() -> None:
    """Pin bare-exempt membership so a new exempt type requires an explicit tier decision."""
    assert PLAN_REVIEW_BARE_EXEMPT_TYPES == {"session_log", "code_review", "identity"}
    assert PLAN_REVIEW_BUG_TIER_TYPES == {"bug"}
    assert "bug" not in PLAN_REVIEW_BARE_EXEMPT_TYPES
