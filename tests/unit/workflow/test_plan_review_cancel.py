"""Mid-run cancellation of a plan review when the ticket's OWN material changes (story 2c89).

The cancel predicate is scoped to the subject's OWN material fingerprint only — never
tracker HEAD, the relation snapshot, or related_material — so an unrelated ticket's
concurrent write can never cancel (the d70a guard). The probe runs at exactly two
between-pass seams (after finders / before verify, and after decide / before coach),
reads under ``local_read_context`` (no fetch/reconverge), and a cancelled run yields an
unsigned INDETERMINATE with NO sidecar (a sidecar write would advance the store revision).
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import attest, context_assembly, generation
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import gate_dispatch, plan_review_recovery
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import AgentStepRunner, StepResult

_TARGET = "T-1"

_GOOD_AC = (
    "## Why\nusers need X\n\n## What\nbuild X in src/x.py\n\n## Scope\njust X\n\n"
    "## Acceptance Criteria\n"
    "- [ ] X returns 200 on the happy path\n"
    "- [ ] a unit test covers the error path (`pytest -q`)\n\n"
    "## Verification\nRun the x test module.\n"
)


def _doc() -> dict:
    from rebar.llm.workflow.gate_dispatch import _gate_doc

    return _gate_doc("plan-review", None)


def _state(*, description: str = _GOOD_AC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": "story",
        "status": "open",
        "title": "Build X",
        "description": description,
        "file_impact": [{"path": "src/x.py", "reason": "the X change"}],
    }


def _patch_reads(monkeypatch, state: dict) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])


class _CountingFinder(FakeRunner):
    """The Pass-1 finder seam (ProductionBatchRunner drives it); counts calls."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def run(self, req):
        self.calls += 1
        return super().run(req)


class _CannedAgent(AgentStepRunner):
    """No-token agent runner for the prerequisite-verify / verify / coach prompt steps."""

    def __init__(self):
        self.prompts_seen: list[str] = []

    def run(self, ctx) -> StepResult:
        prompt = ctx.step.get("prompt")
        self.prompts_seen.append(prompt)
        if prompt in (
            "plan-review-verifier",
            "plan-review-verifier-agentic",
            "plan-review-prerequisite-verifier",
        ):
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
    def __init__(self):
        self.store: dict[str, dict] = {}

    def run_started(self, record): ...

    def run_finished(self, record): ...

    def step_recorded(self, record):
        self.store[record["frame_key"]] = dict(record)

    def completed_step(self, run_id, frame_key):
        return None


def _finder() -> _CountingFinder:
    return _CountingFinder(
        structured={
            "analysis": "",
            "findings": [{"finding": "f-E1", "criteria": ["E1"]}],
        }
    )


def _run(monkeypatch, state, *, finder, agent):
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner

    rec = _Rec()
    res = _ex.run_workflow(
        _doc(),
        {"ticket_id": _TARGET, "probe_criteria": []},
        recorder=rec,
        target_ticket=_TARGET,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=agent,
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    return rec, res


# ── the seam probes ───────────────────────────────────────────────────────────


def test_own_material_change_cancels_at_post_finders_seam(monkeypatch):
    """A fingerprint flip observed at the verify_inputs seam skips verify/decide/coach."""
    state = _state()
    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: "CHANGED"
    )
    agent = _CannedAgent()
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None) as scope:
        _rec, res = _run(monkeypatch, state, finder=_finder(), agent=agent)
    assert scope.event.is_set(), "the probe must set the cancel event"
    assert scope.seam == "post-finders"
    assert res.status == "failed"
    # The Pass-2 verify and Pass-4 coach prompt steps were never invoked.
    assert not any(p and p.startswith("plan-review-verifier") for p in agent.prompts_seen)
    assert "plan-review-coach" not in agent.prompts_seen


def test_own_material_change_cancels_at_post_decide_seam(monkeypatch):
    """A flip first observable after decide cancels before the coach prompt step."""
    state = _state()
    fingerprints = iter(["BASELINE", "CHANGED", "CHANGED", "CHANGED"])
    monkeypatch.setattr(
        attest,
        "current_material_fingerprint",
        lambda tid, repo_root=None: next(fingerprints, "CHANGED"),
    )
    agent = _CannedAgent()
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None) as scope:
        _rec, res = _run(monkeypatch, state, finder=_finder(), agent=agent)
    assert scope.event.is_set()
    assert scope.seam == "post-decide"
    assert res.status == "failed"
    # verify ran (the first probe saw the unchanged baseline); the coach never did.
    assert any(p and p.startswith("plan-review-verifier") for p in agent.prompts_seen)
    assert "plan-review-coach" not in agent.prompts_seen


def test_unrelated_store_write_does_not_cancel_d70a_guard(monkeypatch):
    """The d70a guard: the subject's OWN fingerprint is unchanged (only OTHER tickets'
    material moved), so nothing cancels and the run completes like an undisturbed one."""
    state = _state()
    # The single-ticket probe read returns the (unchanged) baseline throughout — an
    # unrelated ticket's write is invisible to it by construction.
    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: "BASELINE"
    )
    agent = _CannedAgent()
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None) as scope:
        _rec, res = _run(monkeypatch, state, finder=_finder(), agent=agent)
    assert not scope.event.is_set()
    assert res.status == "succeeded", res.error
    assert any(p and p.startswith("plan-review-verifier") for p in agent.prompts_seen)


def test_probe_read_error_fails_open_never_cancels(monkeypatch):
    """Monotone safety: an unreadable fingerprint (None) never cancels — the run
    proceeds and any real staleness is caught by the sign-time re-check."""
    state = _state()
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda tid, repo_root=None: None)
    agent = _CannedAgent()
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None) as scope:
        _rec, res = _run(monkeypatch, state, finder=_finder(), agent=agent)
    assert not scope.event.is_set()
    assert res.status == "succeeded", res.error


def test_no_active_scope_leaves_run_untouched(monkeypatch):
    """Without a cancel scope (e.g. non-gate callers of the ops) the probes are no-ops
    and the run is byte-identical to today's."""
    state = _state()
    agent = _CannedAgent()
    _rec, res = _run(monkeypatch, state, finder=_finder(), agent=agent)
    assert res.status == "succeeded", res.error


def test_probe_reads_under_local_read_context_no_fetch(monkeypatch):
    """The probe never fetches/reconverges: its single-ticket read runs under
    local_read_context, which makes ensure_fresh return before any sync machinery."""
    from rebar._engine_support import reads as ticket_reads

    synced: list[str] = []
    # The sync internals ensure_fresh would reach if the local-read guard failed.
    monkeypatch.setattr(
        ticket_reads,
        "_sync_disabled",
        lambda root=None: synced.append("reached") or False,
    )

    calls: list[bool] = []

    def _probe_fingerprint(tid, repo_root=None):
        # Record that the read happens inside the local-read context by invoking
        # ensure_fresh exactly as a store read would: it must return WITHOUT touching
        # the sync path (_sync_disabled above records any reach-through).
        ticket_reads.ensure_fresh("/nonexistent-tracker")
        calls.append(True)
        return "CHANGED"

    monkeypatch.setattr(attest, "current_material_fingerprint", _probe_fingerprint)
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None):
        with pytest.raises(generation.PlanReviewCancelledStale):
            generation.probe_cancel("post-finders")
    assert calls, "the probe must perform its single-ticket read"
    assert synced == [], "the probe read must not reach the fetch/reconverge machinery"


def test_chunk_funnel_short_circuits_when_cancelled(monkeypatch):
    """pass1._chunk returns empty without a runner call once the event is set."""
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.pass1 import run_pass1

    state = _state()
    _patch_reads(monkeypatch, state)
    ctx = context_assembly.assemble_context(_TARGET, repo_root=None)
    single, agent_criteria = orchestrator.route_criteria(ctx)
    finder = _finder()
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    coverage: dict[str, Any] = {}
    with generation.cancel_scope(_TARGET, "BASELINE", repo_root=None) as scope:
        scope.event.set()
        findings = run_pass1(ctx, cfg, finder, single, agent_criteria, coverage)
    assert finder.calls == 0, "a cancelled funnel must not reach the runner"
    assert [f for f in findings if not f.get("_shed")] == []


# ── the cancelled verdict (gate_dispatch + review_plan tail) ──────────────────


def _plan_ctx() -> PlanContext:
    return PlanContext(
        ticket_id=_TARGET,
        ticket_type="story",
        title="Build X",
        description=_GOOD_AC,
        state=_state(),
    )


def test_produce_returns_cancelled_verdict_before_recoveries(monkeypatch):
    """A cancelled run yields the plan-review-cancelled-stale INDETERMINATE — never the
    coach/verify recovery reconstructions and never a PASS."""
    state = _state()
    _patch_reads(monkeypatch, state)
    fingerprints = iter(["CHANGED"])
    monkeypatch.setattr(
        attest,
        "current_material_fingerprint",
        lambda tid, repo_root=None: next(fingerprints, "CHANGED"),
    )

    def _fake_run_workflow(doc, inputs, **kw):
        # Simulate the interpreter: the seam op raises, is captured in-band, and the
        # remaining steps are skipped -> a failed RunResult.
        try:
            generation.probe_cancel("post-finders")
        except generation.PlanReviewCancelledStale as exc:
            return SimpleNamespace(
                run_id="r",
                workflow_name="plan-review",
                status="failed",
                outputs={},
                terminal_step=None,
                terminal_output=None,
                error=f"step 'verify_inputs' failed: {exc}",
                steps={},
            )
        raise AssertionError("the probe must cancel under a changed fingerprint")

    monkeypatch.setattr("rebar.llm.workflow.executor.run_workflow", _fake_run_workflow)
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    verdict = gate_dispatch.produce_plan_review_verdict(
        _plan_ctx(), cfg, runner=FakeRunner(), advisory_cap=10, repo_root=None
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["indeterminate"][0]["id"] == "plan-review-cancelled-stale"
    assert verdict["coverage"]["cancelled"]["seam"] == "post-finders"
    assert verdict["signature"] == {"signed": False, "reason": "cancelled-stale"}
    assert verdict["sidecar_emitted"] is False
    assert verdict["coverage"]["llm_ran"] is False


def test_run_plan_review_returns_cancelled_verdict_verbatim(monkeypatch):
    """The review_plan tail skips the floors, signing, and the sidecar emit for a
    cancelled verdict — no sidecar write, no attestation (monotone: withhold-only)."""
    from rebar.llm import plan_review as pr

    cancelled = plan_review_recovery._cancelled_plan_review_verdict(
        _plan_ctx(),
        dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5"),
        scope=SimpleNamespace(seam="post-finders"),
    )
    monkeypatch.setattr(
        gate_dispatch, "produce_plan_review_verdict", lambda *a, **k: dict(cancelled)
    )
    snapshot = SimpleNamespace(
        prerequisite_ids=[],
        ticket_states_by_id={},
        related_material=(),
        ticket_store_revision="rev",
    )
    monkeypatch.setattr(
        "rebar.llm.plan_review.relation_snapshot.collect_plan_relation_snapshot",
        lambda tid, repo_root=None, ignore_untracked=False: snapshot,
    )
    monkeypatch.setattr(
        generation,
        "from_snapshot",
        lambda snap: SimpleNamespace(
            phase="planning", priority_floor=None, own_material="BASELINE"
        ),
    )
    monkeypatch.setattr(
        "rebar.llm.plan_review.context_assembly.assemble_context",
        lambda tid, repo_root=None, cfg=None: _plan_ctx(),
    )
    signed: list = []
    emitted: list = []
    monkeypatch.setattr(attest, "sign_plan_review", lambda *a, **k: signed.append(1) or {})
    monkeypatch.setattr(
        "rebar.llm.plan_review.sidecar.emit", lambda *a, **k: emitted.append(1) or True
    )
    verdict = pr._run_plan_review(
        _TARGET,
        cfg=dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5"),
        runner=None,
        sign=True,
        emit_sidecar=True,
        advisory_cap=None,
        repo_root=None,
        force=True,
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["indeterminate"][0]["id"] == "plan-review-cancelled-stale"
    assert verdict["signature"]["signed"] is False
    assert verdict["sidecar_emitted"] is False
    assert signed == [], "a cancelled review must never sign"
    assert emitted == [], "a cancelled review must never emit a sidecar"
    # The CLI maps a non-retryable INDETERMINATE to exit 2 (the existing mapping).
    from rebar._cli import _llm_commands

    assert _llm_commands._disposition_exit_code(verdict, indeterminate_code=2) == 2
