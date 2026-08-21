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
    # `performance` is a still-advisory overlay (b9c0 flipped only `security` to blocking).
    assert reg.threshold_for(["performance"]) == (0.95, False)


def test_threshold_for_unknown_criterion_is_the_default():
    assert reg.threshold_for(["totally-unknown"]) == (0.95, False)


def test_threshold_for_takes_min_threshold_and_any_blocking():
    # min over thresholds; blocking_enabled True iff ANY criterion is blocking-enabled.
    bt, blocking = reg.threshold_for(["security", "secret-detection"])
    assert bt == 0.5  # secret-detection's lower threshold wins (min)
    assert blocking is True  # secret-detection is blocking-enabled (WS5 flipped it)
    # a purely-advisory criterion set stays non-blocking (performance + docs are both advisory;
    # b9c0 flipped only `security`, so this pair no longer includes a blocking criterion).
    assert reg.threshold_for(["performance", "docs"]) == (0.95, False)


def test_secrets_security_keys_are_the_ws5_blocking_handoff():
    """The WS2->WS5 contract: WS2 OWNS the two detector criterion keys (shipped them with
    blocking_enabled=False); WS5 flips EXACTLY these two to True (the only blocking-enabled
    criteria). Post-WS5 they read True; threshold_for reflects it."""
    idx = reg.routing_index()
    for key in ("secret-detection", "high-critical-security"):
        assert idx[key]["blocking_enabled"] is True, f"{key} is the WS5-flipped blocking criterion"
    assert reg.threshold_for(["high-critical-security"])[1] is True
    # b9c0 (2026-07-12) added `security` as the first serious-tier AGENT blocking criterion, at
    # the 9f25-derived threshold. ticket cranial-goodly-seahog then flipped four more from the
    # code-v3 sidecar calibration (api-compat@0.51, deletion-impact@0.60, regression@0.54,
    # error-handling@0.50); bug obese-dihedral-ermine flipped `tests`@0.54 from the code-v4
    # replay. The approved blocking set is now exactly these eight, and adding a ninth must be
    # a deliberate, re-approved change.
    blocking = [k for k, v in idx.items() if v.get("blocking_enabled")]
    assert set(blocking) == {
        "secret-detection",
        "high-critical-security",
        "security",
        "api-compat",
        "deletion-impact",
        "regression",
        "error-handling",
        "tests",
    }


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
    from rebar.llm.prompting.prompts import get_prompt

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
    # emits the code-review-specific verifier contract (kernel Verification shape EXTENDED with the
    # consequence binaries + detection judgment decide.impact_code reads; story albite-lazy-barb).
    assert "outputs: code_review_verification" in body


# ── ticket 5452-3077-b34a-4157: version-signal gating + removed-public-symbol sub-question ──
def _code_severity_model():
    from rebar.llm.review_kernel import verify_models

    model = verify_models.code_review_verification_model()
    verification = model.model_fields["verifications"].annotation.__args__[0]
    return verification.model_fields["severity_attributes"].annotation


def test_unversioned_break_is_version_signal_gated_on_both_surfaces():
    """a2: `unversioned_published_contract_break` fires ONLY when the break lacks a
    version/deprecation signal — the field description AND the prompt bullet must both name
    the three signals and the managed-removal=>FALSE rule (the two pass-2 surfaces the
    verifier reads)."""
    desc = _code_severity_model().model_fields["unversioned_published_contract_break"].description
    body = pathlib.Path("src/rebar/llm/reviewers/code-review-verify.md").read_text()
    for surface in (desc, body):
        for token in ("major version bump", "deprecation cycle", "CHANGELOG breaking-change"):
            assert token in surface, f"missing version-signal token {token!r}"
        assert "managed" in surface.lower()


def test_verify_prompt_keys_removed_public_symbol_on_export():
    """proposal-3: the removed-public-symbol sub-question keys detection on the EXPORT —
    `__all__`/re-export, CLI command, MCP tool — explicitly NOT on a cited caller (the absence
    of an internal caller does not prove an external API unused)."""
    fields = _code_severity_model().model_fields
    assert fields["removed_public_symbol"].default is False  # abstain-safe
    assert fields["version_signal_present"].default is False
    body = pathlib.Path("src/rebar/llm/reviewers/code-review-verify.md").read_text()
    assert "removed_public_symbol" in body
    assert "version_signal_present" in body
    for token in ("__all__", "CLI command", "MCP tool"):
        assert token in body, f"export-keyed detection token {token!r} missing"
    assert "does not prove" in body  # the internal-caller non-proof rule
    desc = fields["removed_public_symbol"].description
    assert "__all__" in desc and "caller" in desc


def test_all_code_review_prompts_are_canonical_front_matter_fixed_points():
    """Guard: every code-review-*.md must be a front-matter FIXED POINT (the CI gate
    test_prompt_front_matter asserts this for ALL packaged prompts; pin it here so a new/edited
    code-review prompt with hand-wrapped front-matter is caught in this story's own suite)."""
    from rebar.llm.prompting.prompts_frontmatter import _split_front_matter_raw, write_front_matter

    for p in sorted(pathlib.Path("src/rebar/llm/reviewers").glob("code-review-*.md")):
        text = p.read_text(encoding="utf-8")
        assert write_front_matter(*_split_front_matter_raw(text)) == text, (
            f"{p.name} front-matter is not canonical — re-run write_front_matter round-trip"
        )


def test_coach_contract_registered_with_move_pick_shape():
    from rebar.llm import contracts
    from rebar.llm.prompting.prompts import get_prompt

    assert get_prompt("code-review-coach").outputs == "code_review_coach"
    model = contracts.response_model_for("code_review_coach")
    assert model.__name__ == "CodeCoachOutput"
    # the nested CodeCoachNote carries move_id/subject/finding_refs
    inst = model(
        notes=[{"move_id": "extract-helper", "subject": "the parser", "finding_refs": ["0"]}]
    )
    assert inst.notes[0].move_id == "extract-helper"


# ── bug obese-dihedral-ermine: fire case blocks end-to-end through the REAL packaged routing ──
def test_tests_criterion_blocks_at_packaged_threshold():
    # The `tests` criterion is flipped to blocking@0.54 in the packaged criteria_routing.json.
    bt, blocking = reg.threshold_for(["tests"])
    assert bt == 0.54
    assert blocking is True


def test_fire_case_forbids_contract_allowed_state_blocks_e2e():
    """END-TO-END: a Pass-1 `tests` finding that the verifier grounded as a contract-contradicting
    assertion (forbids_contract_allowed_state=True) on a load-bearing guarded path (prod_impact
    medium) that is silent, decided through the REAL packaged routing (registry.threshold_for)
    and the REAL impact_code, yields a BLOCK. This is the bug's fire case (ticket 7f9f).
    """
    from rebar.llm.review_kernel.decide import impact_code, pass3_over_findings

    findings = [
        {
            "id": "0",
            "finding": "test forbids a state the contract declares allowed",
            "criteria": ["tests"],
        }
    ]
    verifs = {
        0: {
            # a well-grounded finding: every graded binary answered yes => validity 1.0.
            "binary": {
                "is_verifiable": "yes",
                "evidence_entails_finding": "yes",
                "path_reachable": "yes",
                "impact_follows_necessarily": "yes",
                "no_viable_alternative_explanation": "yes",
                "no_existing_mitigation": "yes",
                "severity_claim_justified": "yes",
            },
            "severity_attributes": {
                "forbids_contract_allowed_state": True,
                "prod_impact": "medium",
                "silent_failure": True,
            },
        }
    }
    out = pass3_over_findings(
        findings, verifs, threshold_for=reg.threshold_for, impact_fn=impact_code
    )
    # forbids_contract_allowed_state is serious maint (0.9) * amp 1.0 (silent) = 0.9 impact;
    # validity 1.0 => priority 0.9 >= packaged tests@0.54 => BLOCK.
    assert out[0]["impact"] == 0.9
    assert out[0]["priority"] == 0.9
    assert out[0]["decision"] == "block"


def test_tightening_pass_case_does_not_block_e2e():
    """The pass case: a deliberate tightening (no forbids_contract_allowed_state) with an
    already-detected, non-silent moderate gap tops out at 0.48 < 0.54 => advisory, not block.
    """
    from rebar.llm.review_kernel.decide import impact_code, pass3_over_findings

    findings = [{"id": "0", "finding": "coverage gap", "criteria": ["tests"]}]
    verifs = {
        0: {
            "binary": {
                "is_verifiable": "yes",
                "evidence_entails_finding": "yes",
                "path_reachable": "yes",
                "impact_follows_necessarily": "yes",
                "no_viable_alternative_explanation": "yes",
                "no_existing_mitigation": "yes",
                "severity_claim_justified": "yes",
            },
            "severity_attributes": {
                "reachable_path_without_automated_coverage": True,
                "prod_impact": "medium",
                # NOT silent => amp 0.8 => 0.48
            },
        }
    }
    out = pass3_over_findings(
        findings, verifs, threshold_for=reg.threshold_for, impact_fn=impact_code
    )
    assert out[0]["impact"] == 0.48
    assert out[0]["decision"] == "advisory"
