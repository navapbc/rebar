"""Deterministic (offline) lifecycle tests for the R4 lightweight BUG REVIEW TIER (epic 6982).

Before R4, the plan-review gate short-circuited EVERY bug to a bare exempt-PASS
(`workflow_ops.plan_review_precheck` → `orchestrator._exempt_verdict`), so a bug got no
substantive review. The bug tier instead runs the DET floor + the advisory `necessity` probe
(`registry.BUG_TIER_CRITERIA`) and NEVER blocks a bug. These tests pin, with NO live LLM:

* precheck: a bug emits ``run_llm=True`` + ``det_blocking==[]`` + ``coverage.bug_tier`` (and is
  NOT the bare exempt short-circuit), while session_log/code_review/identity STAY exempt;
* routing: a bug's included LLM criteria are restricted to ``BUG_TIER_CRITERIA`` (= necessity),
  and every bug-tier criterion is advisory (so the tier is structurally non-blocking);
* an end-to-end OFFLINE run on a bug produces a PASS verdict with ``runner != "exempt"``, no
  blocking findings, and the necessity finding surfaced as advisory.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.plan_review import registry
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import STEP_REGISTRY, AgentStepRunner, StepContext, StepResult

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")
_TARGET = "BUG-1"
_BUG_DESC = (
    "## Reproduction Steps\n1. do X.\n2. observe Y.\n\n"
    "**Expected:** Z.\n**Actual:** not Z.\n\n"
    "## What\nFix the handler in `src/rebar/x.py`.\n\n"
    "## Acceptance Criteria\n- [ ] Y no longer happens, covered by a test.\n"
)


def _state(*, ttype: str, description: str = _BUG_DESC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Some ticket",
        "description": description,
        "deps": [],
    }


def _patch_reads(monkeypatch, state: dict) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])


def _ctx(state: dict, *, step_id: str) -> StepContext:
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


# ── registry: the bug tier is a restricted, advisory set ─────────────────────────────────────
def test_bug_tier_criteria_are_necessity_and_advisory():
    assert registry.BUG_TIER_CRITERIA == ("necessity",)
    by_id = registry.by_id(None)
    for cid in registry.BUG_TIER_CRITERIA:
        # Structural "never blocks a bug": every bug-tier criterion ships advisory posture.
        assert by_id[cid]["default_posture"] == "advisory", cid


# ── precheck: a bug gets the light tier, not the bare exempt short-circuit ───────────────────
def test_precheck_bug_runs_light_tier_never_blocking(monkeypatch):
    op = STEP_REGISTRY["plan_review_precheck"]
    _patch_reads(monkeypatch, _state(ttype="bug"))
    out = op(_ctx(_state(ttype="bug"), step_id="precheck"))
    # The LLM tier runs (not the exempt short-circuit) ...
    assert out["run_llm"] is True
    assert out["verdict"] is None
    # ... but a bug is NEVER blocked: all DET findings are downgraded to advisory.
    assert out["det_blocking"] == []
    assert out["det_coverage"].get("bug_tier") is True


@pytest.mark.parametrize("ttype", ["session_log", "code_review", "identity"])
def test_precheck_other_exempt_types_stay_exempt(monkeypatch, ttype):
    op = STEP_REGISTRY["plan_review_precheck"]
    _patch_reads(monkeypatch, _state(ttype=ttype))
    out = op(_ctx(_state(ttype=ttype), step_id="precheck"))
    assert out["run_llm"] is False
    assert out["verdict"]["runner"] == "exempt"
    assert out["verdict"]["verdict"] == "PASS"


# ── assemble: a bug's included LLM criteria are restricted to the bug tier ────────────────────
def test_assemble_bug_restricts_included_to_bug_tier(monkeypatch):
    op = STEP_REGISTRY["plan_review_assemble_criteria"]
    _patch_reads(monkeypatch, _state(ttype="bug"))
    out = op(_ctx(_state(ttype="bug"), step_id="assemble"))
    included = {cid for cid, on in out.items() if cid.startswith("include_") and on}
    assert included == {"include_necessity"}, included
    routed = out["routing"]["single_turn"] + out["routing"]["agent_tier"]
    assert routed == ["necessity"], routed


# ── end-to-end OFFLINE run on a bug → PASS, non-blocking, runner != exempt ────────────────────
class _CannedAgent(AgentStepRunner):
    def __init__(self):
        self.prompts_seen: list[str] = []

    def run(self, ctx) -> StepResult:
        prompt = ctx.step.get("prompt")
        self.prompts_seen.append(prompt)
        if prompt and prompt.startswith("plan-review-verifier"):
            findings = ctx.inputs.get("findings") or []
            verifs = [
                {
                    "index": i,
                    "severity_attributes": {
                        "prod_impact": "low",
                        "debt_impact": "low",
                        "blast_radius": "local",
                        "likelihood": "low",
                        "reversibility": "easy",
                    },
                    "binary": {
                        "cited_reference_accurate": "na",
                        "is_verifiable": "yes",
                        "evidence_entails_finding": "yes",
                        "path_reachable": "yes",
                        "impact_follows_necessarily": "yes",
                        "no_viable_alternative_explanation": "yes",
                        "no_existing_mitigation": "yes",
                        "severity_claim_justified": "yes",
                    },
                }
                for i in range(len(findings))
            ]
            return StepResult(outputs={"verifications": verifs}, status="succeeded")
        if prompt == "plan-review-coach":
            return StepResult(outputs={"notes": []}, status="succeeded")
        return StepResult(outputs={"_fake": True}, status="succeeded")


class _Rec(_ex.RunRecorder):
    def __init__(self):
        self.store: dict = {}

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _terminal_verdict(rec) -> dict | None:
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "coverage" in out and "ticket_id" in out:
            return out
    return None


def test_e2e_offline_bug_gets_advisory_review_not_exempt(monkeypatch):
    """The whole point of R4 piece (b): a bug now gets a substantive advisory review
    (runner != 'exempt') instead of the bare exempt-PASS, and is never blocked."""
    state = _state(ttype="bug")
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner

    # The finder fires the necessity probe (the only included bug-tier criterion).
    finder = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": "necessity nit", "criteria": ["necessity"]}],
        }
    )
    doc = _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))
    rec = _Rec()
    res = _ex.run_workflow(
        doc,
        {"ticket_id": _TARGET, "probe_criteria": []},
        recorder=rec,
        target_ticket=_TARGET,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=_CannedAgent(),
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    assert res.status == "succeeded", res.error
    verdict = _terminal_verdict(rec)
    assert verdict is not None
    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == []
    # NOT the bare exempt short-circuit — a real (advisory) review ran.
    assert verdict["runner"] != "exempt"
