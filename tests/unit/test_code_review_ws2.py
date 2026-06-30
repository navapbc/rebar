"""WS2 (epic b744): overlay catalog + criteria_routing.json + threshold_for + move-catalog +
verify/coach prompts. Pins the cross-story contracts (the WS5 blocking_enabled handoff) and the
kernel-consumer plug-ins.
"""

from __future__ import annotations

import pathlib

import pytest

from rebar.llm.code_review import moves
from rebar.llm.code_review import registry as reg

pytestmark = pytest.mark.unit


# ── criteria_routing.json: every overlay + the two detector keys, well-formed ──────────────
def test_routing_index_covers_every_overlay_and_detector_keys():
    idx = reg.routing_index()
    for oid in reg.OVERLAY_IDS:
        assert oid in idx, f"overlay {oid!r} has no criteria_routing.json entry"
        entry = idx[oid]
        assert set(entry) >= {
            "exec",
            "applies_to",
            "default_posture",
            "block_threshold",
            "blocking_enabled",
        }
        assert isinstance(entry["applies_to"], list)
        assert isinstance(entry["blocking_enabled"], bool)
    # the two WS5 detector keys exist
    assert "secret-detection" in idx and "high-critical-security" in idx


# ── threshold_for BEHAVIOR (not just key existence) ────────────────────────────────────────
def test_threshold_for_default_overlay_is_advisory_at_095():
    assert reg.threshold_for(["security"]) == (0.95, False)


def test_threshold_for_unknown_criterion_is_the_default():
    assert reg.threshold_for(["totally-unknown"]) == (0.95, False)


def test_threshold_for_takes_min_threshold_and_any_blocking():
    # min over thresholds; blocking_enabled True iff ANY criterion is blocking-enabled
    bt, blocking = reg.threshold_for(["security", "secret-detection"])
    assert bt == 0.5  # secret-detection's lower threshold wins (min)
    assert blocking is False  # both ship blocking_enabled=False in v1


def test_secrets_security_keys_ship_disabled_for_ws5_handoff():
    """The WS2->WS5 contract: WS2 ships the detector keys with blocking_enabled=False; WS5 flips
    EXACTLY these two to True. A test pins they exist and are disabled today."""
    idx = reg.routing_index()
    for key in ("secret-detection", "high-critical-security"):
        assert idx[key]["blocking_enabled"] is False, f"{key} must ship disabled (WS5 flips it)"
    # threshold_for reflects the disabled posture
    assert reg.threshold_for(["high-critical-security"])[1] is False


def test_applies_to_globs_single_source_and_escalation_only():
    assert reg.applies_to_globs("security")  # has globs
    assert reg.applies_to_globs("performance") == []  # escalation-only (no broad glob)
    assert reg.applies_to_globs("unknown") == []


# ── move-catalog: validates at load; applies_when vocabulary; kernel renders deterministically ─
def test_move_catalog_validates_and_uses_closed_applies_when_vocabulary():
    mr = moves.load_move_registry()
    assert mr  # non-empty + validate_move_registry didn't raise
    allowed = set(reg.OVERLAY_IDS) | {"always"}
    for mid, m in mr.items():
        assert "{subject}" in m["template"], f"move {mid} template missing {{subject}}"
        for tag in m.get("applies_when", []):
            assert tag in allowed, (
                f"move {mid} applies_when tag {tag!r} not in OVERLAY_IDS ∪ always"
            )


def test_kernel_coach_renders_a_picked_move_template_deterministically():
    from rebar.llm import review_kernel

    mr = moves.load_move_registry()
    surviving = [{"id": "0", "finding": "no test for the new branch", "criteria": ["tests"]}]

    def _pick(_instructions, applicable):
        # the LLM would pick; here we deterministically pick add-regression-test
        assert "add-regression-test" in applicable  # applicable given active_triggers={tests}
        return [
            {"move_id": "add-regression-test", "subject": "the new branch", "finding_refs": ["0"]}
        ]

    notes = review_kernel.coach(surviving, mr, pick=_pick, active_triggers={"tests"})
    assert len(notes) == 1
    # the prose is rendered from the move template — deterministic {subject} substitution
    assert notes[0]["coaching"] == "Add a regression test covering the new branch."


def test_security_only_move_is_filtered_out_when_not_triggered():
    from rebar.llm import review_kernel

    mr = moves.load_move_registry()
    surviving = [{"id": "0", "finding": "x", "criteria": ["docs"]}]
    picked = {}

    def _pick(_instructions, applicable):
        picked["applicable"] = set(applicable)
        return []

    review_kernel.coach(surviving, mr, pick=_pick, active_triggers={"docs"})
    # threat-model (applies_when=[security]) must NOT be offered for a docs-only change
    assert "threat-model" not in picked["applicable"]
    assert "update-docs" in picked["applicable"]  # applies_when includes docs


# ── prompts: overlays + verify + coach resolve with the right contract/category ─────────────
def test_overlay_prompts_resolve_as_code_review_pass_finders():
    from rebar.llm.prompts import get_prompt

    for oid in reg.OVERLAY_IDS:
        p = get_prompt(f"code-review-{oid}")
        assert p.outputs == "code_review_findings"
        assert p.category == "code-review-pass"
        assert not p.is_reviewer  # stays out of the single-pass reviewer catalog


def test_verify_prompt_embeds_verifier_rules_scaffold_and_regrounds_on_diff():
    from rebar.llm.review_kernel import VERIFIER_RULES_SCAFFOLD

    body = pathlib.Path("src/rebar/llm/reviewers/code-review-verify.md").read_text()
    assert VERIFIER_RULES_SCAFFOLD in body, (
        "verify prompt must embed VERIFIER_RULES_SCAFFOLD verbatim"
    )
    assert "{{ticket_context}}" in body  # re-grounds against the diff context
    assert "outputs: verification" in body  # reuses the kernel's gate-agnostic contract


def test_all_code_review_prompts_are_canonical_front_matter_fixed_points():
    """Guard: every code-review-*.md must be a front-matter FIXED POINT (the CI gate
    test_prompt_front_matter asserts this for ALL packaged prompts; pin it here so a new/edited
    code-review prompt with hand-wrapped front-matter is caught in this story's own suite)."""
    from rebar.llm.prompts_frontmatter import _split_front_matter_raw, write_front_matter

    for p in sorted(pathlib.Path("src/rebar/llm/reviewers").glob("code-review-*.md")):
        text = p.read_text(encoding="utf-8")
        assert write_front_matter(*_split_front_matter_raw(text)) == text, (
            f"{p.name} front-matter is not canonical — re-run write_front_matter round-trip"
        )


def test_coach_contract_registered_with_move_pick_shape():
    from rebar.llm import contracts
    from rebar.llm.prompts import get_prompt

    assert get_prompt("code-review-coach").outputs == "code_review_coach"
    model = contracts.response_model_for("code_review_coach")
    assert model.__name__ == "CodeCoachOutput"
    # the nested CodeCoachNote carries move_id/subject/finding_refs
    inst = model(
        notes=[{"move_id": "extract-helper", "subject": "the parser", "finding_refs": ["0"]}]
    )
    assert inst.notes[0].move_id == "extract-helper"
