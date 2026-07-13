"""HELD-OUT edge tests for d4cf (G7 leaf-parent-containment). Merge into
tests/unit/test_plan_review_g7.py after the implementer has only seen the happy path."""

import pytest

from rebar.llm.criteria import overlay as _overlay
from rebar.llm.plan_review import registry

pytestmark = pytest.mark.unit


def _g7() -> dict:
    for c in registry.load_criteria():
        if c["id"] == "G7":
            return c
    raise AssertionError("G7 not found")


def test_require_parent_id_absent_is_permissive() -> None:
    # A criterion WITHOUT require_parent_id (every pre-existing one) is unaffected: it applies
    # regardless of whether the ticket has a parent (default has_parent=False must not gate them).
    e1 = next(c for c in registry.load_criteria() if c["id"] == "E1")
    assert registry.applies(e1, has_children=False, has_parent=False) is True
    assert registry.applies(e1, has_children=False, has_parent=True) is True


def test_applies_default_has_parent_false_does_not_break_existing() -> None:
    # Calling applies() the OLD way (no has_parent kwarg) must still work for non-G7 criteria.
    g5 = next(c for c in registry.load_criteria() if c["id"] == "G5")
    assert registry.applies(g5, has_children=True) in (True, False)  # no TypeError


def test_require_parent_id_false_is_a_valid_bool() -> None:
    _overlay._validate_routing_entry(
        "project.y",
        {"exec": "AGENT", "applies_at": {"scope": ["leaf"], "require_parent_id": False}},
        where="test",
    )


def test_require_parent_id_int_rejected() -> None:
    with pytest.raises(_overlay.CriteriaError):
        _overlay._validate_routing_entry(
            "project.y",
            {"exec": "AGENT", "applies_at": {"scope": ["leaf"], "require_parent_id": 1}},
            where="test",
        )


def test_g7_scope_is_leaf_only() -> None:
    g7 = _g7()
    scope = (g7.get("applies_at") or {}).get("scope")
    assert scope == ["leaf"]


def test_g7_requires_parent_in_routing() -> None:
    g7 = _g7()
    assert (g7.get("applies_at") or {}).get("require_parent_id") is True


def test_g7_rubric_front_matter_is_agentic_criterion() -> None:
    from importlib import resources

    body = (
        resources.files("rebar.llm.reviewers")
        .joinpath("plan_review_G7.md")
        .read_text(encoding="utf-8")
    )
    head = body[:400]
    assert "execution_mode: agentic" in head
    assert "category: plan-review-criterion" in head


def test_g7_rubric_conveys_subset_containment_and_parent_realign() -> None:
    from importlib import resources

    body = (
        resources.files("rebar.llm.reviewers")
        .joinpath("plan_review_G7.md")
        .read_text(encoding="utf-8")
        .lower()
    )
    # containment semantics: a leaf must be a SUBSET of the parent; narrowing is fine
    assert "subset" in body or "narrow" in body
    # the productive move when the parent is wrong: update the PARENT first, never diverge silently
    assert "update the parent" in body or "parent first" in body


def test_g7_appears_in_the_generated_criteria_guide() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    guide = root / "docs" / "plan-review-criteria-guide.md"
    assert guide.exists()
    assert "## G7" in guide.read_text(encoding="utf-8")


def test_g7_promotion_doc_names_fire_rate_and_norm_id() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "experiments" / "plan-review-gate" / "g7-promotion-path.md").read_text(
        encoding="utf-8"
    )
    low = doc.lower()
    assert "fire-rate" in low or "fire rate" in low
    assert "g7" in low
