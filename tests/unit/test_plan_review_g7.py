"""Happy-path tests for the G7 leaf-parent-containment plan-review criterion (d4cf).

G7 is an AGENT-tier, advisory criterion that fires ONLY on a leaf ticket WITH a parent and
checks that the leaf's scope is a SUBSET of its parent's declared scope (parent wins on
conflict). It reads the parent via the always-available show_ticket tool at runtime — no new
context plumbing — so it is AGENT_TIER but NOT CODEBASE_GROUNDED."""

import pytest

from rebar.llm.criteria import overlay as _overlay
from rebar.llm.plan_review import passes, registry

pytestmark = pytest.mark.unit


def _g7() -> dict:
    """Load the merged G7 descriptor from the live registry."""
    for c in registry.load_criteria():
        if c["id"] == "G7":
            return c
    raise AssertionError("G7 not found in the registry")


def test_g7_registered_agent_tier_not_codebase_grounded() -> None:
    assert "G7" in registry.CANONICAL_LLM
    assert "G7" in registry.AGENT_TIER
    assert "G7" not in registry.CODEBASE_GROUNDED
    assert registry.exec_tier(_g7()) == "AGENT"


def test_g7_posture_is_advisory_at_0_95() -> None:
    g7 = _g7()
    assert g7["default_posture"] == "advisory"
    assert g7["block_threshold"] == 0.95


def test_g7_routing_fires_on_leaf_with_parent_only() -> None:
    g7 = _g7()
    # leaf WITH parent → fires
    assert registry.applies(g7, has_children=False, has_parent=True, ticket_type="task") is True
    # leaf WITHOUT parent → skipped (require_parent_id)
    assert registry.applies(g7, has_children=False, has_parent=False, ticket_type="task") is False
    # container (has children) → skipped (scope=leaf)
    assert registry.applies(g7, has_children=True, has_parent=True, ticket_type="story") is False
    # bug / session_log leaf-with-parent → skipped (suppress_types)
    assert registry.applies(g7, has_children=False, has_parent=True, ticket_type="bug") is False
    assert (
        registry.applies(g7, has_children=False, has_parent=True, ticket_type="session_log")
        is False
    )


def test_require_parent_id_axis_validated_as_bool() -> None:
    # A bool require_parent_id is accepted; a non-bool is a located load-time error.
    _overlay._validate_routing_entry(
        "project.x",
        {"exec": "AGENT", "applies_at": {"scope": ["leaf"], "require_parent_id": True}},
        where="test",
    )
    with pytest.raises(_overlay.CriteriaError):
        _overlay._validate_routing_entry(
            "project.x",
            {"exec": "AGENT", "applies_at": {"scope": ["leaf"], "require_parent_id": "yes"}},
            where="test",
        )


def test_validate_packaged_routing_accepts_g7() -> None:
    assert registry.validate_packaged_routing() == []


def test_registry_coverage_still_passes_with_g7() -> None:
    ok, missing = registry.check_registry_coverage()
    assert ok, f"registry missing: {missing}"


def test_g7_coach_move_realign_to_parent() -> None:
    moves = passes.MOVE_REGISTRY
    realign = [m for m in moves.values() if "G7" in (m.get("applies_when") or [])]
    assert realign, "no parent-realign coach move gated on G7"
    template = realign[0]["template"].lower()
    assert "parent" in template


def test_g7_rubric_states_parent_wins_and_show_ticket() -> None:
    from importlib import resources

    body = (
        resources.files("rebar.llm.reviewers")
        .joinpath("plan_review_G7.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "parent wins" in body
    assert "show_ticket" in body


def test_g7_promotion_path_doc_exists_and_documents_the_path() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    doc = root / "docs" / "experiments" / "plan-review-gate" / "g7-promotion-path.md"
    assert doc.exists(), f"missing {doc}"
    text = doc.read_text(encoding="utf-8")
    assert "plan_review_result_v1" in text  # the fire-rate query is over these sidecars
    assert "0.6" in text and "0.7" in text  # the promotion block band
