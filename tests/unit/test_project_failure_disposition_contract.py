"""Deterministic (no-LLM) tests for the project-scoped advisory plan-review criterion
`project.failure-disposition-contract` (incident 1c0d prevention, ticket
c789-e1bb-4c7b-496c).

Mirrors the `necessity` built-in's offline pins (tests/unit/test_necessity_criterion.py) and
the `project.portability` eval-corpus pins (tests/unit/test_project_portability_eval.py), but
for a PROJECT criterion authored in the `.rebar/` overlay (NOT a packaged built-in):

* registration invariants — activated project criterion, advisory, single-turn (1-TURN),
  facet project-invariants, scope container+leaf; NOT canonical / agent-tier / codebase-
  grounded / bug-tier; carries no `suppress_types`;
* the reviewer rubric's prompt-contract front-matter + the two GATE/REQ checklist sub-answers
  + the explicit T5b anti-double-flag clause;
* the hand-authored bounded-sanity eval corpus shape and its calibration arithmetic under an
  injected perfect solve (the CI-provable proxy: recall 1.0 / false-accept 0.0 over the
  TP/TN fixtures — the LIVE rubric behaviour is the operator-attested billable run, evidenced
  by the committed baseline JSON);
* the escalated-bug-vs-light-bug-tier routing (route_criteria includes it for a non-test
  file_impact bug and excludes it for a test-only bug — the ticket's "verify this" clause);
* parity gates stay clean (packaged routing + criteria guide have no orphan);
* zero-wiring auto-inclusion in the standing effectiveness recorder.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals import eval as _eval
from rebar.llm.plan_review import registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.orchestrator import bug_blast_radius_escalates, route_criteria

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_REPO = str(_ROOT)
_CID = "project.failure-disposition-contract"
_PID = "plan-review-project-failure-disposition-contract"
_RUBRIC = _ROOT / ".rebar" / "prompts" / f"{_PID}.md"
_EVAL_FILE = _ROOT / ".rebar" / "evals" / f"{_PID}.eval.yaml"
_BASELINE = (
    _ROOT
    / "docs"
    / "experiments"
    / "plan-review-gate"
    / "runs"
    / ("failure_disposition_sanity.json")
)

_FIRE_IDS = {"FDC-TP1", "FDC-TP2", "FDC-TP3"}
_PASS_IDS = {"FDC-TN1", "FDC-TN2", "FDC-TN3", "FDC-TN4"}


# ── registration invariants ─────────────────────────────────────────────────────────────────
def test_activated_project_criterion_advisory_single_turn():
    # Activated by the `.rebar/criteria_routing.json` overlay's `activate` map (presence in the
    # file is not enough).
    assert _CID in registry.effective_criteria(_REPO)
    # A PROJECT criterion — deliberately NOT a packaged built-in / canonical id.
    assert _CID not in registry.CANONICAL_LLM
    # A single-turn plan-text probe — NOT code-grounded, NOT agent-tier (contrast R1/A1).
    assert _CID not in registry.CODEBASE_GROUNDED
    assert _CID not in registry.AGENT_TIER
    desc = registry.by_id(_REPO)[_CID]
    assert registry.exec_tier(desc) == "1-TURN"
    assert desc["default_posture"] == "advisory"  # ships advisory — never blocks
    assert desc["facet"] == "project-invariants"
    assert desc["applies_at"]["scope"] == ["container", "leaf"]


def test_routing_entry_shape_advisory_no_suppress_and_activated():
    routing = registry.effective_routing(_REPO)
    entry = routing[_CID]
    assert entry["exec"] == "1-TURN"
    assert entry["default_posture"] == "advisory"
    assert entry["facet"] == "project-invariants"
    assert entry["applies_at"]["scope"] == ["container", "leaf"]
    # No DET detector in v1 (applicability is LLM-judged from the rubric, like necessity).
    assert "trigger" not in entry
    # Deliberately DOES NOT suppress "bug" — an escalated bug (non-test file_impact, like the
    # 8fbd origin) is reviewed under the full rubric and picks this up.
    assert "bug" not in (entry.get("applies_at", {}).get("suppress_types") or [])
    # The overlay file itself lists it under `activate: [...plan_review]`.
    overlay = json.loads((_ROOT / ".rebar" / "criteria_routing.json").read_text(encoding="utf-8"))
    assert overlay["activate"][_CID] == ["plan_review"]
    assert _CID in overlay["plan_review"]


def test_not_in_bug_tier():
    # NOT a bug-tier criterion (the light bug tier runs only registry.BUG_TIER_CRITERIA).
    assert _CID not in registry.BUG_TIER_CRITERIA


def test_checklist_is_gate_then_contract():
    routing = registry.effective_routing(_REPO)
    keys = {c["key"] for c in routing[_CID]["checklist"]}
    # Two criterion-local sub-answers: the applicability GATE, then the contract requirement.
    assert keys == {"affects_failure_disposition", "disposition_contract_stated"}


# ── prompt-contract front-matter + rubric content ────────────────────────────────────────────
def test_prompt_contract_front_matter_and_rubric_body():
    assert criterion_prompt_id(_CID) == _PID
    body = _RUBRIC.read_text(encoding="utf-8")
    fm = yaml.safe_load(body.split("---")[1])
    assert fm["execution_mode"] == "single_turn"
    assert fm["category"] == "plan-review-criterion"
    assert fm["dimension"] == "project-invariants"
    # The two GATE/REQ sub-answers are named in the rubric prose.
    assert "affects_failure_disposition" in body
    assert "disposition_contract_stated" in body
    # Advisory posture + the promotion gate are documented in the rubric.
    assert "ADVISORY" in body
    assert "docs/plan-review-gate.md" in body
    # Explicit orthogonality clause against T5b (avoid double-flagging).
    assert "T5b" in body
    # rebar-specific disposition vocabulary the rubric keys applicability on.
    for token in ("retryable", "fatal", "fallback", "exit-11", "classify_llm_failure"):
        assert token in body, f"rubric missing disposition vocab {token!r}"


# ── eval corpus (bounded hand-authored sanity fixtures) ──────────────────────────────────────
def _spec() -> dict:
    return _eval.load_eval_spec(_PID, repo_root=_REPO)


def test_eval_override_resolution_and_validates():
    p = _eval.eval_spec_path(_PID, repo_root=_REPO)
    assert p == _EVAL_FILE
    assert p.is_file()
    spec = _spec()
    assert spec["prompt"] == _PID
    assert _eval.validate_eval_spec(spec) == []  # no validation errors
    det = [s for s in spec["scorers"] if s.get("type") == "deterministic"]
    assert len(det) == 1
    assert det[0]["name"] == "emits_valid_findings"


def test_eval_corpus_shape_bounded_and_balanced():
    spec = _spec()
    ds = {c["id"]: c["expect"] for c in spec["dataset"]}
    assert {cid for cid, e in ds.items() if e == "finding"} == _FIRE_IDS
    assert {cid for cid, e in ds.items() if e == "pass"} == _PASS_IDS
    # Bounded: total live single-criterion runs stays <= 8 (NOT an E2/E3 batch eval).
    assert len(spec["dataset"]) <= 8
    assert len(spec["dataset"]) == len(_FIRE_IDS) + len(_PASS_IDS)


def test_calibration_arithmetic_under_perfect_solve():
    # The CI-PROVABLE PROXY: an injected perfect solve makes recall 1.0 / false-accept 0.0 by
    # construction — this exercises the corpus TP/TN shape + the calibration arithmetic, NOT the
    # live rubric's classification (that is the operator-attested billable run, evidenced by the
    # committed baseline JSON below).
    def _perfect_solve(pid, case):
        fires = case.get("expect") in ("finding", "fail")
        return {"findings": [{"criteria": [_CID]}] if fires else []}

    r = _eval.calibrate_criterion(_CID, repo_root=_REPO, solve=_perfect_solve, runs=3)
    assert (r["n_fire"], r["n_nofire"]) == (len(_FIRE_IDS), len(_PASS_IDS))
    assert r["recall"] == 1.0
    assert r["false_accept"] == 0.0
    assert r["agreement"] == 1.0
    assert r["kappa"] == pytest.approx(1.0)
    assert r["stability_min"] == pytest.approx(1.0)


def test_committed_baseline_shape():
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    assert base["recall_over_positives"] == 1.0
    assert base["false_accept_over_negatives"] == 0.0
    cases = {c["id"]: c for c in base["cases"]}
    assert set(cases) == _FIRE_IDS | _PASS_IDS
    for cid in _FIRE_IDS:
        assert cases[cid]["expected_fire"] is True
        assert cases[cid]["observed_fire"] is True
    for cid in _PASS_IDS:
        assert cases[cid]["expected_fire"] is False
        assert cases[cid]["observed_fire"] is False


# ── routing: fires on task + escalated bug, silent on the light bug tier ──────────────────────
def _plan_ctx(*, ticket_type: str, file_impact: list[str]) -> PlanContext:
    plan = (
        "## Why\nThe classify_llm_failure fallback chain needs a stated disposition.\n\n"
        "## What\nAlter the retry/fallback/timeout handling on the provider path.\n\n"
        "## Acceptance Criteria\n- [ ] the failure path behaves, covered by a test\n"
    )
    return PlanContext(
        ticket_id="FDC-ROUTE",
        ticket_type=ticket_type,
        title="failure disposition routing probe",
        description=plan,
        state={"ticket_id": "FDC-ROUTE", "ticket_type": ticket_type, "file_impact": file_impact},
        repo_root=_REPO,
    )


def _routed_ids(**kw) -> set[str]:
    single, agent = route_criteria(_plan_ctx(**kw))
    return {c["id"] for c in [*single, *agent]}


def test_routes_on_task_plan():
    assert _CID in _routed_ids(ticket_type="task", file_impact=["src/rebar/llm/x.py"])


def test_escalated_bug_routes_it_light_bug_tier_does_not():
    non_test = ["rebar.toml"]  # the 8fbd-shaped config flip — a NON-test path escalates
    test_only = ["tests/unit/test_x.py"]  # a test-only bug stays in the light advisory tier
    assert bug_blast_radius_escalates(non_test) is True
    assert bug_blast_radius_escalates(test_only) is False
    # Escalated bug (non-test file_impact): reviewed under the FULL rubric (ticket_type=None) →
    # the criterion routes.
    assert _CID in _routed_ids(ticket_type="bug", file_impact=non_test)
    # Light bug tier (test-only file_impact): only BUG_TIER_CRITERIA run → the criterion is
    # excluded, and it never sees raw claim-time bug content.
    assert _CID not in _routed_ids(ticket_type="bug", file_impact=test_only)


# ── parity gates stay clean (no orphan introduced) ───────────────────────────────────────────
def test_parity_gates_clean():
    # The packaged routing + the auto-generated criteria guide are CANONICAL_LLM-only: a project
    # criterion must NOT introduce an orphan in either.
    assert registry.validate_packaged_routing() == []
    assert registry.validate_criteria_guide(_REPO) == []


def test_explain_renders_from_rubric():
    # A project criterion is documented via `rebar explain`, rendered from the overlay rubric,
    # NOT via a `## <id>` guide section.
    assert registry.explain_criterion(_CID, repo_root_path=_REPO).startswith(f"## {_CID}")


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
                "severity": "major",
                "priority": 0.6,
                "norm_id": "n-fdc-1",
                "drop_reason": None,
            }
        ],
    }
    rows = ce.firings_from_review(
        "tkt-fdc",
        1_000,
        "round-1",
        payload,
        fix_unit_key=lambda f: "u-fdc",
        norm_id=lambda f: f.get("norm_id", "n"),
    )
    metrics = ce.compute_effectiveness(rows, window=None)
    assert _CID in metrics
    assert metrics[_CID]["sample_counts"]["advisory_firings"] == 1
