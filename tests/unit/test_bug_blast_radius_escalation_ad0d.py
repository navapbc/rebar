"""ad0d B1: a bug whose PERSISTED file_impact declares any NON-TEST path ESCALATES out of
the light advisory bug tier (epic 6982/R4) into the full blocking rubric.

The escalation keys on the ticket's declared blast radius — derived deterministically at
review time by ``orchestrator.bug_blast_radius_escalates`` (no diff exists yet) — and lands
at the SINGLE routing seam both enforcement steps share:

* ``plan_review_precheck`` (workflow_ops): an escalated bug stops forcing
  ``det_blocking=[]`` — DET findings keep their real posture and can BLOCK (the DET
  short-circuit applies exactly as for a non-bug ticket); coverage records
  ``bug_tier: False`` + ``bug_blast_escalated: True``.
* ``orchestrator.route_criteria``: an escalated bug routes the FULL criteria set — both the
  ``BUG_TIER_CRITERIA`` restriction and the packaged ``suppress_types: ["bug"]``
  applicability axis are lifted (every full-suite criterion carries that suppression, so
  lifting only the tier restriction would deliver an empty escalation). route_criteria is
  ALSO the batch-runner fan-in seam (production_batch_runner._project_criteria →
  route_criteria), so the route-level tests cover that caller.

A bug with NO file_impact, or a test-only one (``tests/**`` / ``conftest.py``), keeps the
light advisory tier unchanged, and the CLI claim-time bug exemption
(``rebar._commands.gates._PLAN_REVIEW_EXEMPT_TYPES``) is untouched — bugs stay claimable
with no attestation regardless of blast radius. All offline: no live LLM.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.orchestrator import bug_blast_radius_escalates, route_criteria
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review ops
from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

pytestmark = pytest.mark.unit

_TARGET = "BUG-1"

# Passes the whole DET floor (P1 AC checklist, P10 `## Testing`, P11 lexicon) — proven shape
# from tests/unit/workflow/test_plan_review_workflow.py::_GOOD_AC.
_GOOD_DESC = (
    "## Why\nthe system needs X.\n\n## What\nfix the handler in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true, covered by a test\n- [ ] another check\n\n"
    "## Testing\nRun `pytest tests/unit/test_x.py -q`.\n"
)
# Trips the BLOCKING P1 readiness floor: no `## Acceptance Criteria` checklist anywhere.
_NO_AC_DESC = "Just a body: no acceptance-criteria checklist anywhere in this plan."


def _state(*, description: str, file_impact: list[str] | None = None, ttype: str = "bug") -> dict:
    st = {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Some bug",
        "description": description,
        "deps": [],
    }
    if file_impact is not None:
        st["file_impact"] = file_impact
    return st


def _patch_reads(monkeypatch, state: dict) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None, **kw: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None, **kw: [])


def _ctx(*, step_id: str) -> StepContext:
    return StepContext(
        run_id="r",
        step_id=step_id,
        kind="scripted",
        step={},
        inputs={"ticket_id": _TARGET},
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )


def _precheck(monkeypatch, state: dict) -> dict:
    _patch_reads(monkeypatch, state)
    return STEP_REGISTRY["plan_review_precheck"](_ctx(step_id="precheck"))


def _plan_ctx(state: dict) -> PlanContext:
    return PlanContext(
        ticket_id=_TARGET,
        ticket_type=state["ticket_type"],
        title=state["title"],
        description=state["description"],
        state=dict(state),
    )


def _routed_ids(state: dict) -> set[str]:
    single, agent = route_criteria(_plan_ctx(state))
    return {c["id"] for c in [*single, *agent]}


# ── the derivation helper: non-test blast radius, deterministically ──────────────────────────
@pytest.mark.parametrize(
    ("file_impact", "expected"),
    [
        (["src/rebar/x.py"], True),
        (["scripts/deploy.sh"], True),  # scripts are NON-test (B2's shared classification rule)
        (["docs/guide.md"], True),
        (["tests/unit/test_x.py"], False),
        (["tests/unit/test_x.py", "tests/conftest.py"], False),
        (["conftest.py"], False),  # repo-root conftest is test infrastructure
        (["tests/unit/test_x.py", "src/rebar/x.py"], True),  # any non-test path escalates
        ([], False),
        (None, False),
        (["./src/rebar/x.py"], True),  # leading ./ normalized
        (["  "], False),  # whitespace-only entry is not a path
    ],
)
def test_bug_blast_radius_escalates(file_impact, expected):
    assert bug_blast_radius_escalates(file_impact) is expected


# ── precheck: escalation lifts the forced det_blocking=[] downgrade ───────────────────────────
def test_escalated_bug_det_block_is_real_and_short_circuits(monkeypatch):
    """Non-test file_impact + a tripped BLOCKING DET rule (P1) → det_blocking NON-EMPTY and
    the DET short-circuit fires exactly as for a non-bug ticket (BLOCK verdict, no LLM)."""
    out = _precheck(monkeypatch, _state(description=_NO_AC_DESC, file_impact=["src/rebar/x.py"]))
    assert out["det_blocking"], "escalation must lift the forced det_blocking=[] downgrade"
    assert out["run_llm"] is False
    assert out["verdict"] is not None
    assert out["verdict"]["verdict"] == "BLOCK"
    assert out["det_coverage"].get("bug_tier") is False
    assert out["det_coverage"].get("bug_blast_escalated") is True


def test_escalated_bug_clean_plan_runs_full_llm_tier(monkeypatch):
    """Non-test file_impact + a DET-passing plan → the (full) LLM tier runs; the escalation
    is observable in coverage."""
    out = _precheck(monkeypatch, _state(description=_GOOD_DESC, file_impact=["src/rebar/x.py"]))
    assert out["run_llm"] is True
    assert out["verdict"] is None
    assert out["det_blocking"] == []
    assert out["det_coverage"].get("bug_tier") is False
    assert out["det_coverage"].get("bug_blast_escalated") is True


def test_bug_without_file_impact_keeps_light_tier(monkeypatch):
    """No declared blast radius → the light advisory tier, even on a DET-tripping plan
    (deterministic default; B2's Gerrit criterion backstops with the real diff)."""
    out = _precheck(monkeypatch, _state(description=_NO_AC_DESC, file_impact=None))
    assert out["run_llm"] is True
    assert out["det_blocking"] == []
    assert out["det_coverage"].get("bug_tier") is True


def test_bug_with_test_only_file_impact_keeps_light_tier(monkeypatch):
    out = _precheck(
        monkeypatch,
        _state(
            description=_NO_AC_DESC,
            file_impact=["tests/unit/test_x.py", "tests/conftest.py", "conftest.py"],
        ),
    )
    assert out["run_llm"] is True
    assert out["det_blocking"] == []
    assert out["det_coverage"].get("bug_tier") is True


# ── routing: the single seam both assemble AND the batch-runner fan-in consume ────────────────
def test_route_criteria_escalated_bug_routes_full_set():
    """An escalated bug routes the SAME criteria set as an equivalent non-bug leaf — both
    the BUG_TIER_CRITERIA restriction and the packaged suppress_types:["bug"] axis are
    lifted. route_criteria is the batch-runner fan-in seam too, so this covers
    production_batch_runner._project_criteria."""
    bug = _state(description=_GOOD_DESC, file_impact=["src/rebar/x.py"])
    task = _state(description=_GOOD_DESC, file_impact=["src/rebar/x.py"], ttype="task")
    bug_ids = _routed_ids(bug)
    task_ids = _routed_ids(task)
    assert bug_ids == task_ids, (
        f"escalated bug must route the full non-bug set; missing={task_ids - bug_ids}"
    )
    assert len(bug_ids) > len(registry.BUG_TIER_CRITERIA)


def test_route_criteria_non_escalated_bug_keeps_bug_tier():
    for fi in (None, ["tests/unit/test_x.py"]):
        routed = _routed_ids(_state(description=_GOOD_DESC, file_impact=fi))
        assert routed <= set(registry.BUG_TIER_CRITERIA), routed


# ── the claim path is untouched: bugs stay claimable with no attestation ──────────────────────
def test_claim_time_bug_exemption_unchanged(monkeypatch):
    """gates._plan_review_gate_applies keys ONLY on ticket type — a bug is exempt from the
    start-work gate regardless of its declared blast radius."""
    from rebar._commands import gates

    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **kw: True)
    assert gates._plan_review_gate_applies("/tmp", "bug", ticket_id=_TARGET) is False
    assert "bug" in gates._PLAN_REVIEW_EXEMPT_TYPES
