"""B4: the PLANNED-TRACE PARITY harness — the migration's offline safety net.

Validates the new plan-review engine WORKFLOW (B2, `gates/plan-review.yaml` driven by the
B1 `ProductionBatchRunner`) against the BESPOKE gate (`orchestrator.run_review`) by
**planned-trace parity** — NOT result parity (the review is LLM-dependent and variable).
Both paths run OFFLINE over a diverse scenario corpus (`_parity_corpus.py`) with a tracing
fake `rebar.llm.Runner`; the harness normalizes both traces and asserts they are EQUAL
PRE-ESCALATION. CI-runnable: no live calls, no credentials.

The planned trace (pinned format)
---------------------------------
An ordered list of intended execution events, each one of:
  * a DET-check id + status (the deterministic P1-P9 floor), or
  * a finder/verifier/coach LLM call ``(role, sorted criteria, call-mode, INTENDED model)``.
Concurrency ordering noise (the Pass-1 finder thread pool) is normalized by SORTING the
per-criterion finder events; we PIN prompt/role + the sorted criteria + call-mode +
INTENDED model + the DET coverage. Parity is asserted on the PRE-ESCALATION trace only
(offline the fake never signals a context limit, so no size-ladder escalation occurs).

Two-seam capture (per `docs/design/batch-runner-seam.md`)
--------------------------------------------------------
The batch's `finders` step is driven by `ProductionBatchRunner`'s OWN injected
`rebar.llm.Runner`, so the finder calls are NOT visible to the engine's generic
`AgentStepRunner`. The trace is therefore assembled from BOTH seams:
  * the batch's injected `rebar.llm.Runner` (`TracingFakeRunner`) — the FINDERS;
  * the engine `AgentStepRunner` (`PlannedTraceRunner`) — the verify/coach PROMPT steps.
The BESPOKE path drives finders + verify + coach through ONE `rebar.llm.Runner`, so a
single `TracingFakeRunner` captures it whole — the SHARED seam that makes parity meaningful.

The known divergence (reconciled, NOT silently introduced)
----------------------------------------------------------
B2 noted: a P1/P5 DET block short-circuits the WORKFLOW with NO LLM call, whereas bespoke
`run_review` still runs a full LLM pass before merging the DET block. So on P1/P5 block
scenarios the traces deliberately DIVERGE. This is handled explicitly: full parity is
asserted on the non-block (`parity`) scenarios; on `block_divergent` scenarios the harness
asserts EXACTLY the documented divergence (the workflow issues zero LLM events; bespoke
issues finder events) — it is never silently tolerated. P8 (too-big) and bug-exempt are
`block_shared`: BOTH paths skip the LLM, so their traces match (empty finder trace).

A second, smaller seam difference is documented rather than over-asserted: the verify/coach
prompt steps carry NO `model:` override in the workflow YAML (the engine resolves the
model), while the bespoke path threads `cfg`/`verifier_cfg`. The non-laddered aggregate
verify/coach model is therefore NOT part of the pinned planned-trace guarantee — the harness
pins the verify/coach ROLE + presence + call-mode-bearing LADDERED FINDER model, which is
where all the routing/chunking/ladder/shed complexity (the actual safety-net surface) lives.
See the report on ticket `key-gun-morph`.
"""

from __future__ import annotations

import dataclasses
import pathlib
from dataclasses import dataclass

import pytest
import yaml

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import orchestrator
from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import STEP_REGISTRY, AgentStepRunner, StepResult
from rebar.llm.workflow.executor import run_workflow as _engine_run_workflow
from rebar.llm.workflow.trace import PlannedTraceRunner, TracingFakeRunner

from ._parity_corpus import Scenario, corpus

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")
# The Pass-1 ENTRY model both paths start from. The workflow's batch sets cfg.model to the
# YAML model_ladder[0]; we give the bespoke path the SAME entry so the laddered finder
# model is compared apples-to-apples (pre-escalation == the entry model, offline).
_ENTRY_MODEL = "claude-haiku-4-5"

_FINDER_ROLES = ("finder", "container", "isf", "isf_summarize")


def _doc() -> dict:
    return _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))


# ── canned engine AgentStepRunner for the workflow's verify + coach PROMPT steps ──
class _CannedAgent(AgentStepRunner):
    """A no-token agent runner for the verify + coach prompt steps: one (surviving)
    verification per finding, one move pick. Mirrors the B1/B2 canned agent so the
    workflow completes offline with the same surviving-advisory shape as the bespoke
    `TracingFakeRunner` produces."""

    def run(self, ctx) -> StepResult:
        prompt = ctx.step.get("prompt")
        if prompt == "plan-review-verifier":
            from rebar.llm.workflow.trace import _canned_verification

            findings = ctx.inputs.get("findings") or []
            verifs = [_canned_verification(i) for i in range(len(findings))]
            return StepResult(outputs={"verifications": verifs}, status="succeeded")
        if prompt == "plan-review-coach":
            return StepResult(
                outputs={
                    "notes": [{"move_id": "1", "subject": "the planned design", "finding_refs": []}]
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


def _terminal_verdict(rec: _Rec) -> dict | None:
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "coverage" in out and "ticket_id" in out:
            return out
    return None


# ── the canonical (normalized) planned trace ──────────────────────────────────────
@dataclass(frozen=True)
class PlannedTrace:
    finders: tuple  # sorted (role, criteria, call_mode, intended_model)
    verify_present: bool
    coach_present: bool
    det: tuple  # sorted (det_check_id, status) — the DET-floor coverage


def _canonical(
    runner_trace: list[dict], agent_trace: list[dict], verdict: dict | None
) -> PlannedTrace:
    """Normalize the two-seam raw trace into the comparable planned trace. Finder events
    are SORTED to drop the Pass-1 thread-pool ordering noise; verify/coach presence is
    read from EITHER seam (the bespoke `rebar.llm.Runner` role OR the workflow's
    `AgentStepRunner` prompt id); DET coverage comes from the produced verdict."""
    finders = tuple(
        sorted(
            (e["role"], e["criteria"], e["call_mode"], e["model"])
            for e in runner_trace
            if e["role"] in _FINDER_ROLES
        )
    )
    roles = {e.get("role") for e in runner_trace}
    prompts = {e.get("prompt") for e in agent_trace}
    verify_present = "verify" in roles or "plan-review-verifier" in prompts
    coach_present = "coach" in roles or "plan-review-coach" in prompts
    det_map = ((verdict or {}).get("coverage") or {}).get("det") or {}
    det = tuple(sorted((k, v.get("status")) for k, v in det_map.items()))
    return PlannedTrace(finders, verify_present, coach_present, det)


# ── the two offline runs ───────────────────────────────────────────────────────────
def _run_bespoke(scn: Scenario, monkeypatch) -> tuple[PlannedTrace, dict]:
    """The bespoke `orchestrator.run_review` path: ONE tracing `rebar.llm.Runner`
    captures finders + verify + coach (the shared seam)."""
    scn.install(monkeypatch)
    tracer = TracingFakeRunner()
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model=_ENTRY_MODEL)
    ctx = orchestrator.assemble_context(scn.ticket_id, repo_root=None)
    verdict = orchestrator.run_review(ctx, cfg, runner=tracer)
    return _canonical(tracer.trace, [], verdict), verdict


def _run_workflow(scn: Scenario, monkeypatch) -> tuple[PlannedTrace, dict]:
    """The B2 workflow path: a tracing `rebar.llm.Runner` injected into the
    `ProductionBatchRunner` (finders) + a `PlannedTraceRunner` at the engine
    `AgentStepRunner` seam (verify/coach)."""
    scn.install(monkeypatch)
    finder = TracingFakeRunner()
    agent = PlannedTraceRunner(inner=_CannedAgent())
    rec = _Rec()
    res = _engine_run_workflow(
        _doc(),
        {"ticket_id": scn.ticket_id},
        recorder=rec,
        target_ticket=scn.ticket_id,
        scripted_registry=dict(STEP_REGISTRY),
        agent_runner=agent,
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    assert res.status == "succeeded", res.error
    verdict = _terminal_verdict(rec)
    return _canonical(finder.trace, agent.trace, verdict), verdict


_CORPUS = corpus()
_IDS = [s.name for s in _CORPUS]


@pytest.mark.parametrize("scn", _CORPUS, ids=_IDS)
def test_planned_trace_parity(scn: Scenario, monkeypatch):
    b_trace, b_verdict = _run_bespoke(scn, monkeypatch)
    w_trace, w_verdict = _run_workflow(scn, monkeypatch)

    # The verdict string agrees on every scenario (a sanity floor under the trace parity).
    assert b_verdict["verdict"] == scn.expected_verdict, b_verdict
    assert w_verdict["verdict"] == scn.expected_verdict, w_verdict

    # The DET floor is identical machinery on both paths over the same assembled context —
    # so the DET coverage must match on EVERY scenario (block or not).
    assert b_trace.det == w_trace.det, (
        f"{scn.name}: DET coverage diverged\n  bespoke={b_trace.det}\n  workflow={w_trace.det}"
    )

    if scn.kind == "parity":
        # The core safety net: identical PRE-ESCALATION finder traces (role + sorted
        # criteria + call-mode + intended entry model), concurrency-order-normalized.
        assert b_trace.finders, f"{scn.name}: expected a non-empty bespoke finder trace"
        assert b_trace.finders == w_trace.finders, _finder_diff(scn, b_trace, w_trace)
        # Both paths reach a verify + a coach pass (structural parity; the non-laddered
        # verify/coach MODEL is a documented seam difference, not pinned here).
        assert b_trace.verify_present and w_trace.verify_present, scn.name
        assert b_trace.coach_present and w_trace.coach_present, scn.name

    elif scn.kind == "block_divergent":
        # The documented B2 divergence: the WORKFLOW short-circuits with NO LLM call;
        # bespoke `run_review` still runs the LLM pass before merging the DET block.
        assert w_trace.finders == (), f"{scn.name}: workflow must NOT call the LLM on a DET block"
        assert not w_trace.verify_present and not w_trace.coach_present, scn.name
        assert b_trace.finders, (
            f"{scn.name}: bespoke run_review DOES run the LLM pass on a P1/P5 block"
        )

    elif scn.kind == "block_shared":
        # P8 too-big / bug-exempt: BOTH paths skip the LLM, so the traces match (empty).
        assert b_trace.finders == () == w_trace.finders, scn.name
        assert not (b_trace.verify_present or w_trace.verify_present), scn.name
        assert not (b_trace.coach_present or w_trace.coach_present), scn.name

    else:  # pragma: no cover - guards against an un-handled corpus kind
        pytest.fail(f"unknown scenario kind {scn.kind!r}")


def _finder_diff(scn: Scenario, b: PlannedTrace, w: PlannedTrace) -> str:
    only_b = set(b.finders) - set(w.finders)
    only_w = set(w.finders) - set(b.finders)
    return (
        f"{scn.name}: finder planned-trace diverged\n"
        f"  only in bespoke:  {sorted(only_b)}\n"
        f"  only in workflow: {sorted(only_w)}"
    )


# ── the harness is genuinely OFFLINE: zero real model calls ──────────────────────
def test_corpus_is_offline_no_real_model_calls(monkeypatch):
    """Belt-and-braces: forbid the real `get_runner` / pydantic_ai path. If any scenario
    reached a live runner instead of the injected fakes, this would raise."""
    import rebar.llm.runner as _runner

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("a real runner was constructed — the harness must stay offline")

    monkeypatch.setattr(_runner, "PydanticAIRunner", _boom)
    for scn in _CORPUS:
        with monkeypatch.context() as m:
            _run_bespoke(scn, m)
        with monkeypatch.context() as m:
            _run_workflow(scn, m)


# ── pin the corpus diversity (the AC's enumerated shapes are all present) ─────────
def test_corpus_covers_required_shapes():
    kinds = {s.name: s.kind for s in _CORPUS}
    required = {
        "leaf_story",
        "container_epic",
        "overlay_on",
        "overlay_off",
        "code_grounded",
        "isf_linked",
        "missing_ac",
        "child_cycle",
        "oversize_p8",
        "bug_exempt",
    }
    assert required <= set(kinds), required - set(kinds)
    assert "parity" in kinds.values() and "block_divergent" in kinds.values()
    assert "block_shared" in kinds.values()
