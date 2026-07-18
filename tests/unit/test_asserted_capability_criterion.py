"""Deterministic (no-LLM) tests for the R1 `asserted-capability` grounding probe (epic 6982).

These pin the criterion's NON-LLM logic — registration invariants (canonical / code-grounded /
agent-tier, non-orphan routing), the routing/finding shape (advisory posture, criterion-local
checklist sub-answers), the prompt-contract front-matter, the bounded-sanity eval fixture shape,
the criteria-guide section, and the zero-wiring auto-inclusion into the standing effectiveness
recorder. The live grounding behavior is exercised out-of-band by the committed
`criteria eval` sanity artifact (the ticket's proving command), mirroring the family's posture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.plan_review import registry

_ROOT = Path(__file__).resolve().parents[2]
_CID = "asserted-capability"


# ── registration invariants ─────────────────────────────────────────────────────────────────


def test_registered_canonical_code_grounded_agent_tier():
    assert _CID in registry.CANONICAL_LLM
    assert _CID in registry.CODEBASE_GROUNDED
    assert _CID in registry.AGENT_TIER


def test_routing_is_advisory_agent_code_grounding_and_non_orphan():
    # Non-orphan: the packaged-routing parity gate (the check that bit R2/b080) is clean.
    assert registry.validate_packaged_routing() == []
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    entry = routing[_CID]
    assert entry["default_posture"] == "advisory"  # ships advisory — never blocks
    assert entry["exec"] == "AGENT"
    assert entry["facet"] == "codebase-grounding"
    assert entry["applies_at"]["scope"] == ["leaf"]
    assert "bug" in entry["applies_at"]["suppress_types"]


def test_effective_registry_marks_it_agent_tier():
    desc = registry.by_id(None)[_CID]
    assert registry.exec_tier(desc) == "AGENT"


# ── routing / finding shape (criterion-local sub-answers) ────────────────────────────────────


def test_checklist_sub_answer_keys():
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    keys = {c["key"] for c in routing[_CID]["checklist"]}
    # The gate sub-answer + the grounding sub-answer (the two-sub-answer gated-then-grounded shape).
    assert keys == {"asserts_named_module_capability", "asserted_capability_grounded"}


def test_prompt_contract_front_matter():
    prompt_id = criterion_prompt_id(_CID)
    assert prompt_id == "plan-review-asserted-capability"
    body = (_ROOT / "src/rebar/llm/reviewers/plan_review_asserted_capability.md").read_text(
        encoding="utf-8"
    )
    front = yaml.safe_load(body.split("---")[1])
    assert front["execution_mode"] == "agentic"
    assert front["dimension"] == "codebase-grounding"
    assert front["category"] == "plan-review-criterion"
    # Advisory posture is documented in the rubric (the promotion-gate cite).
    assert "ADVISORY" in body and "does NOT block" in body


# ── criteria guide ───────────────────────────────────────────────────────────────────────────


def test_guide_has_section_and_is_parity_clean():
    assert registry.validate_criteria_guide() == []
    guide = (_ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {_CID}" in guide
    assert registry.explain_criterion(_CID).startswith(f"## {_CID}")


# ── bounded-sanity eval fixture shape ────────────────────────────────────────────────────────


def test_eval_fixture_has_three_misses_and_clean_controls():
    spec = yaml.safe_load(
        (_ROOT / "src/rebar/llm/eval_specs/plan-review-asserted-capability.eval.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert spec["prompt"] == "plan-review-asserted-capability"
    dataset = spec["dataset"]
    fire = [c["id"] for c in dataset if c["expect"] == "finding"]
    nofire = [c["id"] for c in dataset if c["expect"] == "pass"]
    # The 3 E1-verified misses are must-fire; >=2 clean controls are must-not-fire.
    assert {"ACAP-F1-dc58", "ACAP-F2-db7b", "ACAP-F3-5886"} <= set(fire)
    assert len(nofire) >= 2
    # Bounded: total live single-criterion runs stays <= 8.
    assert len(dataset) <= 8


# ── zero-wiring auto-inclusion into the standing effectiveness recorder ───────────────────────


def _load_recorder():
    path = _ROOT / "docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py"
    spec = importlib.util.spec_from_file_location("criterion_effectiveness", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_auto_included_in_effectiveness_recorder_with_zero_wiring():
    ce = _load_recorder()
    # One advisory REVIEW_RESULT firing that cites the new criterion — the exact production shape.
    payload = {
        "verdict": "PASS",
        "findings": [
            {
                "criteria": [_CID],
                "decision": "advisory",
                "severity": "major",
                "priority": 0.6,
                "norm_id": "n-acap-1",
                "drop_reason": None,
            }
        ],
    }
    rows = ce.firings_from_review(
        "tkt-acap",
        1_000,
        "round-1",
        payload,
        fix_unit_key=lambda f: "u-acap",
        norm_id=lambda f: f.get("norm_id", "n"),
    )
    metrics = ce.compute_effectiveness(rows, window=None)
    # The recorder auto-includes every criterion id it sees — no per-criterion wiring needed.
    assert _CID in metrics
    assert metrics[_CID]["sample_counts"]["advisory_firings"] == 1
