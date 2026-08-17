"""Deterministic (no-LLM) tests for the `ac-process-gate` acceptance-criterion probe.

Pin the criterion's NON-LLM contract — registration invariants (canonical, single-turn, NOT
code-grounded / NOT agent-tier, non-orphan routing), routing/finding shape (advisory posture,
container+leaf scope, criterion-local gate+judgment checklist), prompt-contract front-matter,
the bounded-sanity eval-fixture shape, the criteria-guide section, and zero-wiring
auto-inclusion into the standing effectiveness recorder. The live process-gate-vs-deliverable
discrimination is exercised out-of-band by the committed `criteria eval` sanity artifact (the
ticket's proving command), mirroring the R1/R3/R4 criterion family's posture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.plan_review import registry

_ROOT = Path(__file__).resolve().parents[2]
_CID = "ac-process-gate"


# ── registration invariants ─────────────────────────────────────────────────────────────────
def test_registered_canonical_single_turn_not_grounded():
    assert _CID in registry.CANONICAL_LLM
    # A single-turn plan-text probe — NOT a code-grounded AGENT-tier criterion.
    assert _CID not in registry.CODEBASE_GROUNDED
    assert _CID not in registry.AGENT_TIER
    desc = registry.by_id(None)[_CID]
    assert registry.exec_tier(desc) == "1-TURN"


def test_routing_is_advisory_container_and_leaf_and_non_orphan():
    # Non-orphan: the packaged-routing parity gate is clean (every canonical id routed, no orphan).
    assert registry.validate_packaged_routing() == []
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    entry = routing[_CID]
    assert entry["default_posture"] == "advisory"  # ships advisory — never blocks
    assert entry["exec"] == "1-TURN"
    assert entry["facet"] == "ac-text-quality"
    # Roll-up ACs live on containers too (e.g. "all children closed"), so both scopes apply.
    assert entry["applies_at"]["scope"] == ["container", "leaf"]


def test_checklist_is_gate_then_deliverable_judgment():
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    keys = {c["key"] for c in routing[_CID]["checklist"]}
    # Two criterion-local sub-answers: the gate (are there ACs) then deliverable-vs-gate.
    assert keys == {"has_acceptance_criteria", "criteria_are_deliverable_not_process_gate"}


# ── prompt-contract front-matter ─────────────────────────────────────────────────────────────
def test_prompt_contract_front_matter():
    assert criterion_prompt_id(_CID) == "plan-review-ac-process-gate"
    body = (_ROOT / "src/rebar/llm/reviewers/plan_review_ac_process_gate.md").read_text(
        encoding="utf-8"
    )
    fm = yaml.safe_load(body.split("---")[1])
    assert fm["execution_mode"] == "single_turn"
    assert fm["category"] == "plan-review-criterion"
    assert fm["dimension"] == "ac-text-quality"
    # The rubric documents its advisory posture + the promotion gate.
    assert "ADVISORY" in body
    assert "docs/plan-review-gate.md" in body


def test_rubric_body_enumerates_reject_and_accept_sets_and_litmus():
    body = (_ROOT / "src/rebar/llm/reviewers/plan_review_ac_process_gate.md").read_text(
        encoding="utf-8"
    )
    low = body.lower()
    # Reject-set exemplars (mechanically enforced by CI or rebar).
    for token in ("child", "tests pass", "plan review", "merged", "trailer"):
        assert token in low, f"reject-set token missing: {token!r}"
    # Accept-set signal + the ticket-agnostic litmus + the anti-FP posture.
    assert "deliverable" in low
    assert "identically" in low  # the "reads identically on an unrelated ticket" litmus
    assert "silence" in low  # err-toward-silence anti-false-positive rule


# ── criteria guide section ───────────────────────────────────────────────────────────────────
def test_criteria_guide_section_present_and_clean():
    checkout = Path(__file__).resolve().parents[2]
    assert registry.validate_criteria_guide(str(checkout)) == []
    guide = (_ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {_CID}" in guide
    assert registry.explain_criterion(_CID).startswith(f"## {_CID}")


# ── bounded-sanity eval-fixture shape ────────────────────────────────────────────────────────
def test_bounded_sanity_fixture_shape():
    spec = yaml.safe_load(
        (_ROOT / "src/rebar/llm/eval_specs/plan-review-ac-process-gate.eval.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert spec["prompt"] == "plan-review-ac-process-gate"
    dataset = spec["dataset"]
    fire = [c["id"] for c in dataset if c["expect"] == "finding"]
    nofire = [c["id"] for c in dataset if c["expect"] == "pass"]
    # >=2 process-gate-AC positives must-fire and >=2 deliverable-AC negatives must-not-fire.
    assert len(fire) >= 2
    assert len(nofire) >= 2
    # Bounded: total live single-criterion runs stays <= 8.
    assert len(dataset) <= 8


# ── zero-wiring auto-inclusion in the standing effectiveness recorder ─────────────────────────
def _load_recorder():
    path = _ROOT / "docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py"
    spec = importlib.util.spec_from_file_location("criterion_effectiveness", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_auto_included_in_effectiveness_recorder_with_zero_wiring():
    ce = _load_recorder()
    payload = {
        "verdict": "PASS",
        "findings": [
            {
                "criteria": [_CID],
                "decision": "advisory",
                "severity": "minor",
                "priority": 0.4,
                "norm_id": "n-apg-1",
                "drop_reason": None,
            }
        ],
    }
    rows = ce.firings_from_review(
        "tkt-apg",
        1_000,
        "round-1",
        payload,
        fix_unit_key=lambda f: "u-apg",
        norm_id=lambda f: f.get("norm_id", "n"),
    )
    metrics = ce.compute_effectiveness(rows, window=None)
    # The recorder auto-includes every criterion id it sees — no per-criterion wiring needed.
    assert _CID in metrics
    assert metrics[_CID]["sample_counts"]["advisory_firings"] == 1
