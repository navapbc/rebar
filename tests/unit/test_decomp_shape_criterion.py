"""Deterministic (no-LLM) tests for the R3 `decomp-shape` container criterion (epic 6982).

These pin the criterion's NON-LLM logic — registration invariants (canonical / agent-tier /
NOT code-grounded / container-fan-out membership, non-orphan routing), the routing/finding shape
(advisory posture, container facet+scope, criterion-local checklist sub-answers), the
prompt-contract front-matter, the eval_solver container arm (a fake runner threads through
`pass1_container`), the bounded-sanity eval fixture shape, the criteria-guide section, and the
zero-wiring auto-inclusion into the standing effectiveness recorder. The live decomposition-shape
behavior is exercised out-of-band by the committed `criteria eval` sanity artifact (the ticket's
proving command), mirroring the family's posture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.plan_review import registry
from rebar.llm.plan_review.pass1 import CONTAINER_CRITERIA

_ROOT = Path(__file__).resolve().parents[2]
_CID = "decomp-shape"


# ── registration invariants ─────────────────────────────────────────────────────────────────


def test_registered_canonical_agent_tier_container_not_code_grounded():
    assert _CID in registry.CANONICAL_LLM
    assert _CID in registry.AGENT_TIER
    # It is a CONTAINER criterion (facet container, like G3/G4), NOT a code-grounding one.
    assert _CID not in registry.CODEBASE_GROUNDED
    # It runs on the dedicated container fan-out (pass1_container), not the normal agent path.
    assert _CID in CONTAINER_CRITERIA


def test_routing_is_advisory_agent_container_and_non_orphan():
    # Non-orphan: the packaged-routing parity gate (the check that bit R2/b080) is clean.
    assert registry.validate_packaged_routing() == []
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    entry = routing[_CID]
    assert entry["default_posture"] == "advisory"  # ships advisory — never blocks (permanent)
    assert entry["exec"] == "AGENT"
    assert entry["facet"] == "container"
    assert entry["applies_at"]["scope"] == ["container"]
    assert "bug" in entry["applies_at"]["suppress_types"]
    assert entry["routing"] == "container"


def test_effective_registry_marks_it_agent_tier():
    desc = registry.by_id(None)[_CID]
    assert registry.exec_tier(desc) == "AGENT"


# ── routing / finding shape (criterion-local sub-answers) ────────────────────────────────────


def test_checklist_sub_answer_keys():
    routing = json.loads(
        (_ROOT / "src/rebar/llm/plan_review/criteria_routing.json").read_text(encoding="utf-8")
    )
    keys = {c["key"] for c in routing[_CID]["checklist"]}
    # The gate sub-answer + the shape-detection sub-answer (gated-then-detected shape).
    assert keys == {"has_container_decomposition", "decomposition_shape_sound"}


def test_prompt_contract_front_matter():
    prompt_id = criterion_prompt_id(_CID)
    assert prompt_id == "plan-review-decomp-shape"
    body = (_ROOT / "src/rebar/llm/reviewers/plan_review_decomp_shape.md").read_text(
        encoding="utf-8"
    )
    front = yaml.safe_load(body.split("---")[1])
    assert front["execution_mode"] == "agentic"
    assert front["dimension"] == "container"
    assert front["category"] == "plan-review-criterion"
    # Advisory posture + both smells are documented in the rubric.
    assert "ADVISORY" in body and "does NOT block" in body
    assert "LAYER-CAKE" in body and "CONSUMED-ARTIFACT-WITHOUT-ORDERING-EDGE" in body


# ── eval_solver container arm (drives pass1_container for a container criterion) ──────────────


def test_eval_solver_runs_container_criterion_via_pass1_container(monkeypatch):
    from rebar.llm.evals import eval_solver

    captured: dict = {}

    def fake_pass1_container(runner, cfg, *, parent_plan, children, criteria, sibling_roster):
        captured["parent_plan"] = parent_plan
        captured["children"] = children
        captured["criteria_ids"] = [c["id"] for c in criteria]
        captured["roster"] = sibling_roster
        return [{"finding": "layer-cake", "criteria": [_CID], "location": "child c1"}]

    # resolve_gate_config touches config, not the model — stub it to a sentinel.
    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config", lambda repo_root: object(), raising=True
    )
    # `passes` is imported inside _run_criterion_case (function-local); patch the source module.
    monkeypatch.setattr(
        "rebar.llm.plan_review.passes.pass1_container", fake_pass1_container, raising=True
    )

    case = {
        "input": "## What\nparent plan",
        "children": [
            {"ticket_id": "c1", "title": "DB layer", "description": "schema only"},
            {"ticket_id": "c2", "title": "UI layer", "description": "frontend only"},
        ],
    }
    out = eval_solver._run_criterion_case(_CID, case, runner=object(), repo_root=None)
    assert out["findings"] and out["findings"][0]["criteria"] == [_CID]
    assert captured["criteria_ids"] == [_CID]
    assert captured["children"] == case["children"]
    assert "c1" in captured["roster"] and "c2" in captured["roster"]


def test_eval_solver_container_criterion_requires_children(monkeypatch):
    from rebar.llm.evals import eval_solver

    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config", lambda repo_root: object(), raising=True
    )
    # No `children` payload -> a clear fixture error, never a silent pass.
    try:
        eval_solver._run_criterion_case(
            _CID, {"input": "parent only"}, runner=object(), repo_root=None
        )
    except ValueError as exc:
        assert "children" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError for a container fixture with no children")


# ── criteria guide ───────────────────────────────────────────────────────────────────────────


def test_guide_has_section_and_is_parity_clean():
    assert registry.validate_criteria_guide() == []
    guide = (_ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    assert f"## {_CID}" in guide
    assert registry.explain_criterion(_CID).startswith(f"## {_CID}")


# ── bounded-sanity eval fixture shape ────────────────────────────────────────────────────────


def test_eval_fixture_has_both_positives_and_clean_negatives():
    spec = yaml.safe_load(
        (_ROOT / "src/rebar/llm/eval_specs/plan-review-decomp-shape.eval.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert spec["prompt"] == "plan-review-decomp-shape"
    dataset = spec["dataset"]
    fire = [c["id"] for c in dataset if c["expect"] == "finding"]
    nofire = [c["id"] for c in dataset if c["expect"] == "pass"]
    # One layer-cake positive + one consumed-artifact-without-ordering positive.
    assert {"DSHAPE-F1-layer-cake", "DSHAPE-F2-consumed-artifact-no-order"} <= set(fire)
    # At least one vertical-slice negative + one well-ordered negative.
    assert len(nofire) >= 2
    # Every container case carries a non-empty `children` list (the container payload).
    for c in dataset:
        assert isinstance(c.get("children"), list) and c["children"], c["id"]
    # Bounded: total live single-criterion container runs stays <= 8.
    assert len(dataset) <= 8


# ── zero-wiring auto-inclusion into the effectiveness recorder ────────────────────────────────


def _load_recorder():
    path = _ROOT / "docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py"
    spec = importlib.util.spec_from_file_location("criterion_effectiveness", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_auto_included_in_effectiveness_recorder_with_zero_wiring():
    ce = _load_recorder()
    # One advisory REVIEW_RESULT firing that cites the new criterion, in the exact production shape.
    payload = {
        "verdict": "PASS",
        "findings": [
            {
                "criteria": [_CID],
                "decision": "advisory",
                "severity": "major",
                "priority": 0.6,
                "norm_id": "n-dshape-1",
                "drop_reason": None,
            }
        ],
    }
    rows = ce.firings_from_review(
        "tkt-dshape",
        1_000,
        "round-1",
        payload,
        fix_unit_key=lambda f: "u-dshape",
        norm_id=lambda f: f.get("norm_id", "n"),
    )
    metrics = ce.compute_effectiveness(rows, window=None)
    # The recorder auto-includes every criterion id it sees — zero per-criterion wiring.
    assert _CID in metrics
    assert metrics[_CID]["sample_counts"]["advisory_firings"] == 1
