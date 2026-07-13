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
    # the fire-rate/validity jq query anchors: the sidecar schema + the fields it reads
    assert "plan_review_result_v1" in text  # the fire-rate query is over these sidecars
    for anchor in (".criteria", ".decision", ".validity"):
        assert anchor in text, f"promotion-path doc missing query anchor {anchor}"
    assert "0.6" in text and "0.7" in text  # the promotion block band
    assert "72b6" in text  # the recording location (the deferred field-data follow-up)


def test_g7_semantic_fixture_cases_cover_the_three_scenarios() -> None:
    """Fixture-driven semantic coverage for G7 (d4cf AC). G7 is an AGENT criterion, so its
    semantics are validated by a gated eval (the same convention G3/G4 use in
    plan-review-container.eval.yaml). This deterministic loader asserts the eval carries the three
    required fixture parent/leaf scenarios with the right expectation: a leaf NARROWING its parent
    PASSES (no fire); a leaf ADDING an out-of-parent-scope deliverable FIRES; a leaf CONTRADICTING a
    parent AC FIRES."""
    from rebar.llm.evals import eval as ev

    spec = ev.load_eval_spec("plan-review-g7")
    assert ev.validate_eval_spec(spec) == []
    assert spec["prompt"] == "plan-review-G7"
    cases = {c["id"]: c for c in spec["dataset"]}
    # narrowing → PASS (G7 must NOT fire)
    assert cases["G7-P-consistent-narrowing"]["expect"] == "pass"
    # exceeding → FINDING (G7 fires on an out-of-parent-scope deliverable)
    assert cases["G7-R-out-of-parent-scope"]["expect"] == "finding"
    # contradicting → FINDING (G7 fires on a leaf contradicting a parent AC)
    assert cases["G7-R-contradicts-parent-ac"]["expect"] == "finding"
    # each fixture case names the G7 criterion + a parent/leaf pair in its input
    for cid in cases:
        assert cases[cid]["criterion"] == "G7"
        assert "PARENT" in cases[cid]["input"] and "LEAF" in cases[cid]["input"]
