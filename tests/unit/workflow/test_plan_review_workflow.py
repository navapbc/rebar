"""B2: the plan-review gate as a v3 engine WORKFLOW (running, offline-testable).

Proves `src/rebar/llm/workflow/gates/plan-review.yaml` + its `uses` ops:
 * it validates + lints clean (v3, prompt-refs resolve);
 * `plan_review_assemble_criteria` routes the criteria (proportionate scrutiny + overlay
   triggering) and emits the per-criterion `include_<ID>` inclusion the batch's `when` reads
   — an overlay criterion is included/excluded per its trigger (the E5 advisory);
 * an end-to-end OFFLINE run (inject the B1 ProductionBatchRunner + a FakeRunner for the
   finder, and a canned AgentStepRunner for verify/coach) produces a plan_review_verdict-shaped
   result — NO live calls;
 * the DET-block short-circuit skips the LLM entirely (no finder/verify/coach calls) and yields
   the blocking verdict, mirroring the B3 completion-gate short-circuit;
 * decide/coach produce the expected verdict shape.

Exact behavioural PARITY with orchestrator.run_review is a SEPARATE story (B4); this proves the
workflow RUNS and produces the right shape.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import lint as _lint
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import AgentStepRunner, StepResult

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")
_TARGET = "T-1"

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)
# A plan that fires the T5a (performance) deterministic overlay trigger.
_PERF_AC = _GOOD_AC + "\nWe must cut the p99 latency on the hot path and add a cache.\n"


def _doc() -> dict:
    return _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))


def _state(*, ttype: str = "story", description: str = _GOOD_AC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Build X",
        "description": description,
        "deps": [],
    }


def _patch_reads(monkeypatch, state: dict) -> None:
    import rebar

    monkeypatch.setattr(rebar, "show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr(rebar, "list_tickets", lambda parent=None, repo_root=None: [])


class _CountingFinder(FakeRunner):
    """A FakeRunner (the Pass-1 finder seam ProductionBatchRunner drives) that counts calls
    so a test can assert the LLM was (or was NOT) reached."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def run(self, req):
        self.calls += 1
        return super().run(req)


class _CannedAgent(AgentStepRunner):
    """A no-token agent runner for the verify + coach PROMPT steps. Branches on the step's
    prompt id: the Pass-2 verifier returns one verification per finding (mid validity → a
    surviving ADVISORY); the Pass-4 coach returns one move pick. Counts calls."""

    def __init__(self):
        self.calls = 0
        # Which verifier prompt id the dynamic Pass-2 branch selected (B5): the agentic
        # variant when any finding is code-grounded, else the single-turn one.
        self.verifier_prompts: list[str] = []

    def run(self, ctx) -> StepResult:
        self.calls += 1
        prompt = ctx.step.get("prompt")
        if prompt in ("plan-review-verifier", "plan-review-verifier-agentic"):
            self.verifier_prompts.append(prompt)
            findings = ctx.inputs.get("findings") or []
            verifs = [
                {
                    "index": i,
                    "severity_attributes": {
                        "prod_impact": "medium",
                        "debt_impact": "medium",
                        "blast_radius": "module",
                        "likelihood": "medium",
                        "reversibility": "moderate",
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
            return StepResult(
                outputs={
                    "notes": [{"move_id": "1", "subject": "the X design", "finding_refs": []}]
                },
                status="succeeded",
            )
        return StepResult(outputs={"_fake": True}, status="succeeded")


class _Rec(_ex.RunRecorder):
    """Captures every frame's recorded outputs (keyed by frame_key) for assertion."""

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
    """The plan_review_verdict the workflow produced (the coach or passthrough arm output) —
    distinguished from intermediate frames by carrying both `verdict` (str) and `coverage`."""
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "coverage" in out and "ticket_id" in out:
            return out
    return None


def _run(monkeypatch, state, *, finder, agent):
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner

    rec = _Rec()
    res = _ex.run_workflow(
        _doc(),
        {"ticket_id": _TARGET},
        recorder=rec,
        target_ticket=_TARGET,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=agent,
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    return rec, res


# ── lint + schema ─────────────────────────────────────────────────────────────
def test_workflow_validates_and_lints():
    doc = _doc()
    assert doc["schema_version"] == "3"
    assert _schema.validate_document(doc) == []
    findings = [
        str(f)
        for f in _lint.lint_workflow(_WF.read_text(), check_prompts=True)
        if f.severity != "warning"
    ]
    assert findings == [], findings


# ── assemble_criteria routing + overlay inclusion (the E5 advisory) ───────────
def test_assemble_criteria_overlay_inclusion(monkeypatch):
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    op = STEP_REGISTRY["plan_review_assemble_criteria"]

    def _assemble(state) -> dict:
        _patch_reads(monkeypatch, state)
        ctx = StepContext(
            run_id="r",
            step_id="assemble",
            kind="scripted",
            step={},
            inputs={"ticket_id": _TARGET},
            workflow={},
            target_ticket=_TARGET,
            repo_root=None,
        )
        return op(ctx)

    # The T5a performance overlay applies at TASK level + fires on a deterministic perf signal.
    # No performance signal → EXCLUDED.
    out_clean = _assemble(_state(ttype="task", description=_GOOD_AC))
    assert out_clean["include_T5a"] is False
    assert "T5a" not in out_clean["routing"]["single_turn"] + out_clean["routing"]["agent_tier"]

    # A performance signal in the plan → the T5a overlay is INCLUDED (deterministic trigger).
    out_perf = _assemble(_state(ttype="task", description=_PERF_AC))
    assert out_perf["include_T5a"] is True
    routed = out_perf["routing"]["single_turn"] + out_perf["routing"]["agent_tier"]
    assert "T5a" in routed
    # A non-overlay mandatory criterion (E1) is always routed; ISF is never a batch criterion.
    assert out_perf["include_E1"] is True
    assert "include_ISF" not in out_perf


# ── end-to-end OFFLINE run → a plan_review_verdict-shaped PASS ────────────────
def test_e2e_offline_produces_verdict(monkeypatch):
    from rebar import schemas
    from rebar.llm.plan_review.orchestrator import assemble_context, route_criteria

    state = _state()
    _patch_reads(monkeypatch, state)
    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    routed_ids = [c["id"] for c in single + agent]
    assert routed_ids, "the routed criteria should be non-empty for a well-formed story"

    finder = _CountingFinder(
        structured={
            "analysis": "",
            "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in routed_ids],
        }
    )
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)

    assert res.status == "succeeded", res.error
    assert finder.calls > 0, "the Pass-1 finder must run on the precheck-passed arm"
    assert canned.calls >= 2, "the verify + coach prompt steps must both run"

    verdict = _terminal_verdict(rec)
    assert verdict is not None
    # Shape: validate against the canonical plan_review_verdict schema.
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    schemas.validator(schemas.PLAN_REVIEW_VERDICT).validate(verdict)
    assert verdict["verdict"] == "PASS"
    assert verdict["ticket_id"] == _TARGET
    assert verdict["blocking"] == []
    assert verdict["advisory"], "advisory findings should survive to the verdict"
    # Pass-4 coaching was rendered from the canned move pick (deterministic template).
    assert verdict["coaching"], "coaching should be rendered from the coach move pick"
    assert verdict["coaching"][0]["move_id"] == "1"
    assert "the X design" in verdict["coaching"][0]["coaching"]
    assert verdict["coverage"]["counts"]["blocking"] == 0


# ── P1/P5 DET block does NOT short-circuit: LLM runs, DET block merged → BLOCK ─
def test_p1_det_block_still_runs_llm_and_blocks(monkeypatch):
    # Reconciled with bespoke run_review (story B5): a P1 DET block (here, NO
    # `## Acceptance Criteria`) does NOT short-circuit the LLM — bespoke run_review only
    # stops before the LLM on a P8-too-big plan. So the four-pass review RUNS and the DET
    # block is merged at decide-time → a BLOCK verdict carrying the P1 block (+ any LLM
    # advisories), NOT a no-LLM deterministic short-circuit.
    state = _state(description="Just a body, no acceptance criteria at all here.")
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)

    assert res.status == "succeeded", res.error
    assert finder.calls > 0, "a P1 block does NOT short-circuit — the LLM still runs (parity)"
    verdict = _terminal_verdict(rec)
    assert verdict is not None
    assert verdict["verdict"] == "BLOCK"
    assert verdict["blocking"], "a DET block must itemize the failing check"
    assert any("P1" in (f.get("criteria") or []) for f in verdict["blocking"])


# ── exempt type short-circuits to PASS (no LLM) ──────────────────────────────
def test_bug_is_exempt_short_circuit(monkeypatch):
    state = _state(ttype="bug", description="A bug, gate-exempt.")
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)
    assert res.status == "succeeded", res.error
    assert finder.calls == 0 and canned.calls == 0
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"
    assert verdict["runner"] == "exempt"


# ── decide op shape (deterministic Pass-3 over batch findings + verifications) ─
def test_decide_op_partitions_findings(monkeypatch):
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    _patch_reads(monkeypatch, _state())
    op = STEP_REGISTRY["plan_review_decide"]
    # Two findings against an always-advisory criterion (E1): high-validity verifications →
    # both survive as ADVISORY (no blocking).
    findings = [
        {"finding": "f0", "criteria": ["E1"]},
        {"finding": "f1", "criteria": ["E1"]},
    ]
    verifs = [
        {
            "index": i,
            "severity_attributes": {
                "prod_impact": "medium",
                "blast_radius": "module",
                "likelihood": "medium",
                "reversibility": "moderate",
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
        for i in range(2)
    ]
    ctx = StepContext(
        run_id="r",
        step_id="decide",
        kind="scripted",
        step={},
        inputs={
            "findings": findings,
            "verifications": verifs,
            "det_blocking": [],
            "det_advisory": [],
        },
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    out = op(ctx)
    assert set(out) >= {"blocking", "surfaced", "overflow", "indeterminate", "dropped"}
    assert out["blocking"] == []
    assert len(out["surfaced"]) == 2
    assert all(f["decision"] == "advisory" for f in out["surfaced"])
    assert all("id" in f for f in out["surfaced"])
