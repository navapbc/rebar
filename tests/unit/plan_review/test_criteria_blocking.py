"""T3 + T10 blocking enablement and the T10 infra DET overlay gate (ticket bfa8).

T3 (integration feasibility) and T10 (infra/IaC) never blocked in ~2,500 runs each —
a configuration artifact (default_posture stayed "advisory"), not a quality signal.
Operator approved enabling blocking for both at a conservative 0.90 pilot threshold.
Plan-review blocking derives from ``default_posture: "blocking"`` +
``block_threshold`` (the ``blocking_enabled`` field is the code-review gate's
convention and does not apply here); a finding blocks when its criterion is
blocking-postured and its priority crosses the threshold — no per-class mechanism.

These tests pin:
* the routing posture: T3 and T10 (ticket bfa8) and T5c (ticket c97a) carry
  default_posture=blocking @ 0.90;
* Pass-3 behavior: a T3/T10 finding at priority 0.92 BLOCKS, one at 0.85 stays
  ADVISORY, through the plan-review ``pass3_over_findings`` wrapper;
* the T10 DET overlay gate (audit: 28% fire rate, 100% strong-finding recall):
  fires on a terraform-touching plan and a GHA-workflow plan, skips a pure-docs
  plan with a ``gate_log`` (``coverage.routing.det_gated``) entry and zero T10
  LLM routing;
* the T10 rubric carries the MAJOR-class severity-guidance sentence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar.llm.criteria.model import threshold_for
from rebar.llm.evals import eval as _eval
from rebar.llm.plan_review import orchestrator, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.registry import _DET_OVERLAY_RULES
from rebar.llm.review_kernel import GRADED_BINARY

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]

_T10_SCOPE_CLAUSE = (
    "This overlay evaluates the DURABLE infrastructure — the IaC/config that persists. "
    "Transient, throwaway experiments used to de-risk a mechanism before committing it — "
    "their creation, isolation, and teardown — are out of scope here."
)
_T10_MAJOR_SAFETY_QUALIFIER = (
    "This exclusion does NOT waive the four MAJOR safety classes: a transient apply that is "
    "destructive with no safeguard, grants wildcard-admin access, or commits a plaintext "
    "secret remains in scope, as does an internet- or untrusted-network-reachable transient "
    "service with unspecified human/admin authentication — flag each case."
)

# An all-"yes" graded binary: validity == 1.0, so the finding's priority equals the
# impact scalar we inject — letting each test place a finding at an exact priority.
_ALL_YES = {q: "yes" for q in GRADED_BINARY}

# A plan with NO infra vocabulary (and none of the other overlay vocabularies): the
# total-vocabulary-absence skip case for the T10 gate.
_PURE_DOCS = (
    "Improve the wording of two user-facing error strings.\n\n"
    "## Acceptance Criteria\n- [ ] both strings read clearly\n"
)

_TERRAFORM_PLAN = (
    "## Approach\n"
    "Provision the staging VPC and RDS instance with terraform; remote state in S3\n"
    "with locking, plan-before-apply in CI.\n"
    "## Acceptance Criteria\n- [ ] staging environment reachable\n"
)

_GHA_PLAN = (
    "## Approach\n"
    "Add a release job under .github/workflows triggered by workflow_dispatch.\n"
    "## Acceptance Criteria\n- [ ] release job runs\n"
)


def _ctx(description: str, *, ttype: str = "task") -> PlanContext:
    return PlanContext(
        ticket_id="abcd-0000-0000-0003",
        ticket_type=ttype,
        title="T",
        description=description,
        state={},
    )


def _routed_ids(description: str, gate_log=None) -> set[str]:
    single, agent = orchestrator.route_criteria(_ctx(description), gate_log=gate_log)
    return {c["id"] for c in single + agent}


def _decided(cid: str, priority: float, monkeypatch: pytest.MonkeyPatch) -> dict:
    """One finding for ``cid`` through the REAL plan-review Pass-3 wrapper.

    The graded binary is all-"yes" (validity 1.0) and the impact model is pinned to
    ``priority``, so the finding's priority is exactly ``priority`` while threshold
    resolution + decision run unmodified against the shipped routing table."""
    monkeypatch.setattr(orchestrator.review_kernel, "impact_plan", lambda attrs: priority)
    finding = {"criteria": [cid], "finding": "fixture finding"}
    verifs = {0: {"binary": dict(_ALL_YES), "severity_attributes": {}}}
    return orchestrator.pass3_over_findings([finding], verifs)[0]


# ── routing posture: blocking @ 0.90 ──────────────────────────────────────────
@pytest.mark.parametrize("cid", ["T3", "T10", "T5c"])
def test_routing_posture_blocking_at_090(cid: str) -> None:
    entry = registry.by_id()[cid]
    assert entry["default_posture"] == "blocking"
    assert entry["block_threshold"] == 0.9

    thr, blocking = threshold_for([cid], registry.by_id(), gate="plan_review")
    assert (thr, blocking) == (0.9, True)


# ── Pass-3: above the bar blocks, below stays advisory ────────────────────────
@pytest.mark.parametrize("cid", ["T3", "T10", "T5c"])
def test_priority_092_blocks_through_pass3(cid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _decided(cid, 0.92, monkeypatch)
    assert d["priority"] == 0.92
    assert d["decision"] == "block"
    assert d["block_threshold"] == 0.9
    assert d["blocking_enabled"] is True


@pytest.mark.parametrize("cid", ["T3", "T10", "T5c"])
def test_priority_085_stays_advisory_through_pass3(
    cid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _decided(cid, 0.85, monkeypatch)
    assert d["priority"] == 0.85
    assert d["decision"] == "advisory"
    assert d["block_threshold"] == 0.9
    assert d["blocking_enabled"] is True


# ── the T10 DET overlay gate ──────────────────────────────────────────────────
def test_t10_rule_fires_on_terraform_plan() -> None:
    assert _DET_OVERLAY_RULES["T10"].fires(_TERRAFORM_PLAN) is True


def test_t10_rule_fires_on_gha_workflow_plan() -> None:
    assert _DET_OVERLAY_RULES["T10"].fires(_GHA_PLAN) is True


def test_t10_rule_skips_pure_docs_plan() -> None:
    assert _DET_OVERLAY_RULES["T10"].fires(_PURE_DOCS) is False


def test_t10_rule_has_no_file_impact_arms() -> None:
    # The audit transcription is text-only: no glob arm, no reason arm.
    rule = _DET_OVERLAY_RULES["T10"]
    assert rule.file_impact_globs == ()
    assert rule.file_impact_reason_re is None


def test_route_criteria_gates_t10_on_pure_docs_plan_with_det_gated_entry() -> None:
    gate_log: dict[str, str] = {}
    routed = _routed_ids(_PURE_DOCS, gate_log=gate_log)
    assert "T10" not in routed  # zero T10 LLM routing on a vocabulary-absent plan
    assert gate_log["T10"] == _DET_OVERLAY_RULES["T10"].name  # the det_gated record


@pytest.mark.parametrize("plan", [_TERRAFORM_PLAN, _GHA_PLAN])
def test_route_criteria_routes_t10_on_infra_plans(plan: str) -> None:
    gate_log: dict[str, str] = {}
    routed = _routed_ids(plan, gate_log=gate_log)
    assert "T10" in routed
    assert "T10" not in gate_log


# ── the rubric's MAJOR-class severity guidance ────────────────────────────────
def test_t10_rubric_contains_major_class_severity_guidance() -> None:
    text = (_ROOT / "src/rebar/llm/reviewers/plan_review_T10.md").read_text(encoding="utf-8")
    assert "ONLY these four MAJOR classes warrant severity >= major" in text
    for phrase in (
        "destructive apply",
        "wildcard/overbroad grant",
        "plaintext secret on a committed path",
        "unauthenticated internet-reachable service",
    ):
        assert phrase in text, phrase


# ── T10 durable/transient partition contract (REB-1538 / REB-1540) ───────────
def _t10_rubric() -> str:
    return (_ROOT / "src/rebar/llm/reviewers/plan_review_T10.md").read_text(encoding="utf-8")


def _assert_scope_then_safety(text: str) -> None:
    assert _T10_SCOPE_CLAUSE in text
    assert _T10_MAJOR_SAFETY_QUALIFIER in text
    scope_end = text.index(_T10_SCOPE_CLAUSE) + len(_T10_SCOPE_CLAUSE)
    safety_start = text.index(_T10_MAJOR_SAFETY_QUALIFIER)
    assert text[scope_end:safety_start].strip() == ""


def _t10_eval_spec() -> dict:
    path = _ROOT / "src/rebar/llm/eval_specs/plan-review-T10.eval.yaml"
    assert path.is_file(), "T10 durable/transient eval spec is missing"
    return _eval.load_eval_spec("plan-review-T10", repo_root=str(_ROOT))


def _normalized_case_text(case: dict) -> str:
    return f"{case.get('note', '')} {case.get('input', '')}".lower().replace("-", " ")


def _matching_case_ids(spec: dict, expect: str, terms: tuple[str, ...]) -> set[str]:
    return {
        str(case["id"])
        for case in spec["dataset"]
        if case.get("expect") == expect
        and all(term in _normalized_case_text(case) for term in terms)
    }


def test_t10_rubric_co_locates_scope_and_major_safety_contract() -> None:
    _assert_scope_then_safety(_t10_rubric())


def test_t10_scope_partition_does_not_delegate_to_another_criterion() -> None:
    text = _t10_rubric()
    _assert_scope_then_safety(text)
    assert set(re.findall(r"\bT\d+\b", text)) == {"T10"}


def test_t10_eval_corpus_covers_each_pass_and_finding_boundary() -> None:
    spec = _t10_eval_spec()
    labels = [case.get("expect") for case in spec["dataset"]]
    assert labels.count("pass") == 5
    assert labels.count("finding") == 5

    pass_boundaries = (
        ("loopback", "teardown"),
        ("durable", "specif"),
        ("private", "scoped", "iam"),
        ("secret manager",),
        ("sandbox", "shared"),
    )
    finding_boundaries = (
        ("durable", "state"),
        ("destructive", "shared"),
        ("wildcard", "admin"),
        ("plaintext", "secret"),
        ("untrusted", "auth"),
    )
    pass_ids = [_matching_case_ids(spec, "pass", terms) for terms in pass_boundaries]
    finding_ids = [_matching_case_ids(spec, "finding", terms) for terms in finding_boundaries]
    assert all(len(ids) == 1 for ids in pass_ids), pass_ids
    assert all(len(ids) == 1 for ids in finding_ids), finding_ids
    assert len(set().union(*pass_ids)) == 5
    assert len(set().union(*finding_ids)) == 5


def test_t10_eval_gold_set_is_balanced_and_well_formed() -> None:
    gold = _t10_eval_spec()["gold_set"]
    assert len(gold) == 10
    assert [item.get("label") for item in gold].count("pass") == 5
    assert [item.get("label") for item in gold].count("finding") == 5
    assert all(isinstance(item.get("input"), str) and item["input"].strip() for item in gold)


def test_t10_eval_scores_recall_and_false_fire() -> None:
    scorer_names = {scorer["name"] for scorer in _t10_eval_spec()["scorers"]}
    assert scorer_names == {
        "no_fire_on_good_cases",
        "recall_on_seeded_defects",
    }


def test_t10_explain_and_generated_guide_render_scope_partition() -> None:
    explanation = registry.explain_criterion("T10")
    _assert_scope_then_safety(explanation)
    assert registry.validate_criteria_guide(str(_ROOT)) == []
    guide = (_ROOT / "docs/plan-review-criteria-guide.md").read_text(encoding="utf-8")
    t10_section = guide.split("## T10", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    _assert_scope_then_safety(t10_section)
