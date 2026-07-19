"""Deterministic (no-LLM) tests for the R4 `necessity` no-op/over-action probe (epic 6982).

Pin the criterion's NON-LLM logic — registration invariants (canonical, single-turn, NOT
code-grounded / NOT agent-tier, non-orphan routing), routing/finding shape (advisory posture,
applies to bugs, criterion-local checklist sub-answers), prompt-contract front-matter, the
bounded-sanity eval-fixture shape, the criteria-guide section, and zero-wiring auto-inclusion
into the standing effectiveness recorder. The live over-action/necessity behavior is exercised
out-of-band by the committed `criteria eval` sanity artifact (the ticket's proving command),
mirroring the R1/R3 family's posture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.plan_review import registry

_ROOT = Path(__file__).resolve().parents[2]
_CID = "necessity"


# ── registration invariants ─────────────────────────────────────────────────────────────────
def test_registered_canonical_single_turn_not_grounded():
    assert _CID in registry.CANONICAL_LLM
    # A single-turn plan-text probe — NOT a code-grounded AGENT-tier criterion (contrast R1).
    assert _CID not in registry.CODEBASE_GROUNDED
    assert _CID not in registry.AGENT_TIER
    desc = registry.by_id(None)[_CID]
    assert registry.exec_tier(desc) == "1-TURN"


def test_routing_is_advisory_applies_to_bugs_and_non_orphan():
    # Non-orphan: the packaged-routing parity gate is clean (no ORPHAN — check bit R2/b080).
    assert registry.validate_packaged_routing() == []
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    entry = routing[_CID]
    assert entry["default_posture"] == "advisory"  # ships advisory — never blocks
    assert entry["exec"] == "1-TURN"
    assert entry["facet"] == "scope-intent"
    assert entry["applies_at"]["scope"] == ["leaf"]
    # Deliberately DOES NOT suppress "bug" — the bug review tier runs necessity on bugs.
    assert "bug" not in entry["applies_at"].get("suppress_types", [])


def test_checklist_is_gate_then_demonstrate():
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    keys = {c["key"] for c in routing[_CID]["checklist"]}
    # Two criterion-local sub-answers: the gate (proposes a change) then the necessity judgment.
    assert keys == {"proposes_a_change", "demonstrates_necessity"}


# ── prompt-contract front-matter ─────────────────────────────────────────────────────────────
def test_prompt_contract_front_matter():
    assert criterion_prompt_id(_CID) == "plan-review-necessity"
    body = (_ROOT / "src/rebar/llm/reviewers/plan_review_necessity.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(body.split("---")[1])
    assert fm["execution_mode"] == "single_turn"
    assert fm["category"] == "plan-review-criterion"
    assert fm["dimension"] == "scope-intent"
    # The rubric documents its advisory posture + the promotion gate.
    assert "ADVISORY" in body
    assert "docs/plan-review-gate.md" in body


# ── criteria guide section ───────────────────────────────────────────────────────────────────
def test_criteria_guide_section_present_and_clean():
    assert registry.validate_criteria_guide() == []
    guide = (_ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {_CID}" in guide
    assert registry.explain_criterion(_CID).startswith(f"## {_CID}")


# ── bounded-sanity eval-fixture shape ────────────────────────────────────────────────────────
def test_bounded_sanity_fixture_shape():
    spec = yaml.safe_load(
        (_ROOT / "src/rebar/llm/eval_specs/plan-review-necessity.eval.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert spec["prompt"] == "plan-review-necessity"
    dataset = spec["dataset"]
    fire = [c["id"] for c in dataset if c["expect"] == "finding"]
    nofire = [c["id"] for c in dataset if c["expect"] == "pass"]
    # >=1 over-action positive must-fire and >=1 well-motivated negative must-not-fire.
    assert len(fire) >= 1
    assert len(nofire) >= 1
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
    # One advisory REVIEW_RESULT that fires and cites the new criterion — exact shape.
    payload = {
        "verdict": "PASS",
        "findings": [
            {
                "criteria": [_CID],
                "decision": "advisory",
                "severity": "major",
                "priority": 0.6,
                "norm_id": "n-nec-1",
                "drop_reason": None,
            }
        ],
    }
    rows = ce.firings_from_review(
        "tkt-nec",
        1_000,
        "round-1",
        payload,
        fix_unit_key=lambda f: "u-nec",
        norm_id=lambda f: f.get("norm_id", "n"),
    )
    metrics = ce.compute_effectiveness(rows, window=None)
    # The recorder auto-includes every criterion id it sees — no per-criterion wiring needed.
    assert _CID in metrics
    assert metrics[_CID]["sample_counts"]["advisory_firings"] == 1
