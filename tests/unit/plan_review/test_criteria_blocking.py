"""T3 + T10 blocking enablement and the T10 infra DET overlay gate (ticket bfa8).

T3 (integration feasibility) and T10 (infra/IaC) never blocked in ~2,500 runs each —
a configuration artifact (default_posture stayed "advisory"), not a quality signal.
Operator approved enabling blocking for both at a conservative 0.90 pilot threshold.
Plan-review blocking derives from ``default_posture: "blocking"`` +
``block_threshold`` (the ``blocking_enabled`` field is the code-review gate's
convention and does not apply here); a finding blocks when its criterion is
blocking-postured and its priority crosses the threshold — no per-class mechanism.

These tests pin:
* the routing posture: T3 and T10 carry default_posture=blocking @ 0.90;
* Pass-3 behavior: a T3/T10 finding at priority 0.92 BLOCKS, one at 0.85 stays
  ADVISORY, through the plan-review ``pass3_over_findings`` wrapper;
* the T10 DET overlay gate (audit: 28% fire rate, 100% strong-finding recall):
  fires on a terraform-touching plan and a GHA-workflow plan, skips a pure-docs
  plan with a ``gate_log`` (``coverage.routing.det_gated``) entry and zero T10
  LLM routing;
* the T10 rubric carries the MAJOR-class severity-guidance sentence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import orchestrator, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.registry import _DET_OVERLAY_RULES
from rebar.llm.review_kernel import GRADED_BINARY

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]

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
@pytest.mark.parametrize("cid", ["T3", "T10"])
def test_routing_posture_blocking_at_090(cid: str) -> None:
    entry = registry.by_id()[cid]
    assert entry["default_posture"] == "blocking"
    assert entry["block_threshold"] == 0.9

    thr, blocking = threshold_for([cid], registry.by_id(), gate="plan_review")
    assert (thr, blocking) == (0.9, True)


# ── Pass-3: above the bar blocks, below stays advisory ────────────────────────
@pytest.mark.parametrize("cid", ["T3", "T10"])
def test_priority_092_blocks_through_pass3(cid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _decided(cid, 0.92, monkeypatch)
    assert d["priority"] == 0.92
    assert d["decision"] == "block"
    assert d["block_threshold"] == 0.9
    assert d["blocking_enabled"] is True


@pytest.mark.parametrize("cid", ["T3", "T10"])
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
