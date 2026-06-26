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

P1/P5 DET blocks — now full PARITY (B5 reconciled the divergence)
-----------------------------------------------------------------
B2/B4 noted a divergence: a P1/P5 DET block short-circuited the WORKFLOW with NO LLM call,
whereas bespoke `run_review` ran a full LLM pass before merging the DET block. Story B5
RECONCILED this — the workflow precheck now only short-circuits the LLM on an exempt type or
a P8-too-big plan (matching bespoke), so a P1/P5 block runs the full review and merges the
DET block at decide-time. The `missing_ac` (P1) and `child_cycle` (P5) scenarios are
therefore `parity` (their verdict is BLOCK, but the planned trace MATCHES). P8 (too-big) and
bug-exempt stay `block_shared`: BOTH paths skip the LLM, so their traces match (empty).

Verify call-mode is now CROSS-ASSERTED for equality (B5): the workflow verify step became
dynamic (a `code_grounded` branch picks the agentic vs single-turn verifier prompt), so the
harness asserts both paths choose the SAME call-mode — no longer the B4 document-the-static-
divergence stance.

A smaller seam difference is documented rather than over-asserted: the verify/coach prompt
steps carry NO `model:` override in the workflow YAML (the engine resolves the model), while
the bespoke path threads `cfg`/`verifier_cfg`. The non-laddered aggregate verify/coach model
is therefore NOT part of the pinned planned-trace guarantee — the harness pins the
verify/coach ROLE + presence + call-mode + the call-mode-bearing LADDERED FINDER model, which
is where all the routing/chunking/ladder/shed complexity (the actual safety-net surface) lives.
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
        if prompt in ("plan-review-verifier", "plan-review-verifier-agentic"):
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
# Both capture seams name the verify call-mode in different vocabularies (the bespoke
# `rebar.llm.Runner` echoes "agent"/"1-shot"; the engine `AgentStepRunner` reads the
# prompt's raw `execution_mode` "agentic"/"single_turn"). Normalize both to one canon so
# the captured call-mode is comparable.
_VERIFY_MODE_CANON = {
    "agent": "agentic",
    "agentic": "agentic",
    "1-shot": "single_turn",
    "single_turn": "single_turn",
}


@dataclass(frozen=True)
class PlannedTrace:
    finders: tuple  # sorted (role, criteria, call_mode, intended_model)
    verify_present: bool
    coach_present: bool
    det: tuple  # sorted (det_check_id, status) — the DET-floor coverage
    verify_modes: tuple  # sorted unique canonical verify call-modes ("agentic"/"single_turn")


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
    _VERIFIER_PROMPTS = ("plan-review-verifier", "plan-review-verifier-agentic")
    roles = {e.get("role") for e in runner_trace}
    prompts = {e.get("prompt") for e in agent_trace}
    verify_present = "verify" in roles or any(p in prompts for p in _VERIFIER_PROMPTS)
    coach_present = "coach" in roles or "plan-review-coach" in prompts
    det_map = ((verdict or {}).get("coverage") or {}).get("det") or {}
    det = tuple(sorted((k, v.get("status")) for k, v in det_map.items()))
    # The verify step's call-mode, captured from whichever seam carried it (bespoke runner
    # role "verify", or the workflow verifier PROMPT step — either the single-turn OR the
    # agentic variant the B5 dynamic-verify branch selected) and canonicalized.
    verify_raw = [e["call_mode"] for e in runner_trace if e.get("role") == "verify"] + [
        e["call_mode"] for e in agent_trace if e.get("prompt") in _VERIFIER_PROMPTS
    ]
    verify_modes = tuple(
        sorted({_VERIFY_MODE_CANON.get(m, m) for m in verify_raw if m is not None})
    )
    return PlannedTrace(finders, verify_present, coach_present, det, verify_modes)


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


def _run_workflow(scn: Scenario, monkeypatch) -> tuple[PlannedTrace, dict, _Rec]:
    """The B2 workflow path: a tracing `rebar.llm.Runner` injected into the
    `ProductionBatchRunner` (finders) + a `PlannedTraceRunner` at the engine
    `AgentStepRunner` seam (verify/coach). Returns the recorder too, so the journaled
    finders `batch_plan` (which carries the budget/shed coverage) can be inspected."""
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
    return _canonical(finder.trace, agent.trace, verdict), verdict, rec


def _workflow_batch_plan(rec: _Rec) -> dict | None:
    """The journaled finders `batch_plan` (the opaque budget/shed/ladder coverage the
    `ProductionBatchRunner` produced) from the recorder, or None if absent."""
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("batch_plan"), dict):
            return out["batch_plan"]
    return None


_CORPUS = corpus()
_IDS = [s.name for s in _CORPUS]


@pytest.mark.parametrize("scn", _CORPUS, ids=_IDS)
def test_planned_trace_parity(scn: Scenario, monkeypatch):
    b_trace, b_verdict = _run_bespoke(scn, monkeypatch)
    w_trace, w_verdict, _ = _run_workflow(scn, monkeypatch)

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

        # Pin the Pass-2 verify CALL-MODE (close the presence-only blind spot). BOTH paths now
        # choose it DYNAMICALLY: bespoke via `pass2_verify(..., agentic=grounded)`, and the
        # WORKFLOW (story B5) via a `code_grounded` branch picking the agentic vs single-turn
        # verifier PROMPT. So CROSS-ASSERT EQUALITY — a regression on EITHER side that flips
        # the call-mode fails here (the B5 upgrade over B4's document-the-divergence stance).
        assert b_trace.verify_modes == w_trace.verify_modes, (
            f"{scn.name}: verify call-mode diverged between paths\n"
            f"  bespoke ={b_trace.verify_modes}\n  workflow={w_trace.verify_modes}"
        )
        # …and where the scenario pins an expected value, both must match it.
        if scn.expected_verify_agentic is not None:
            assert ("agentic" in b_trace.verify_modes) == scn.expected_verify_agentic, (
                f"{scn.name}: verify call-mode {b_trace.verify_modes} disagrees with "
                f"expected agentic={scn.expected_verify_agentic}"
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


# ── corpus DIVERSITY: the scenarios route to genuinely DIFFERENT criteria sets ────
# Goldens captured from `orchestrator.route_criteria` over the production routing index.
# They are intentionally FULL sets (the strongest anti-homogenization assertion): a
# `route_criteria` regression that collapses ticket shapes to one criteria set fails here.
# If the production routing legitimately changes, update these goldens.
_GOLDEN_LEAF = frozenset(
    {"COH", "E1", "E2", "E3", "E5", "F1", "F4", "G5", "G6",
     "T1", "T10", "T11", "T2", "T3", "T4", "T5e", "T6", "T8", "T9"}
)  # fmt: skip
_GOLDEN_OVERLAY_OFF = frozenset(
    {"A1", "COH", "E1", "E2", "E3", "E4", "E5", "E6", "F1", "F4", "G1G2", "G5", "G6",
     "T1", "T10", "T11", "T2", "T3", "T4", "T5b", "T5c", "T5e", "T6", "T8", "T9"}
)  # fmt: skip


def _routed_ids(scn: Scenario, monkeypatch) -> frozenset[str]:
    """The finder-criteria set `route_criteria` selects for a scenario (single + agent
    tiers, ISF excluded as it is fed the linked session log, not a rubric chunk)."""
    scn.install(monkeypatch)
    ctx = orchestrator.assemble_context(scn.ticket_id, repo_root=None)
    single, agent = orchestrator.route_criteria(ctx)
    return frozenset(c["id"] for c in single + agent)


def test_corpus_routes_diverse_criteria(monkeypatch):
    """Parity is only meaningful if the corpus genuinely SPANS distinct routing. Without
    this, a `route_criteria` regression that homogenized every ticket to ONE criteria set
    would keep the equality-based parity assertions green (both paths would simply agree on
    the wrong, collapsed set). These assertions FAIL on such a regression."""
    routes = {}
    for scn in _CORPUS:
        if scn.kind != "parity":
            continue
        with monkeypatch.context() as m:
            routes[scn.name] = _routed_ids(scn, m)

    # (1) The corpus produces at least THREE distinct finder-criteria sets.
    distinct = set(routes.values())
    assert len(distinct) >= 3, f"corpus routing collapsed to {len(distinct)} set(s): {routes}"

    # (2) The overlay is ADDITIVE: overlay_on (a perf task that fires the T5a overlay) routes
    # a STRICT SUPERSET of overlay_off (an otherwise-identical clean task) — exactly +T5a.
    assert routes["overlay_off"] < routes["overlay_on"], (
        f"overlay_on must strictly contain overlay_off\n  on ={sorted(routes['overlay_on'])}"
        f"\n  off={sorted(routes['overlay_off'])}"
    )
    assert routes["overlay_on"] - routes["overlay_off"] == {"T5a"}, (
        routes["overlay_on"] - routes["overlay_off"]
    )

    # (3) Per-scenario GOLDENS (spot-check 2 scenarios) — the exact expected criteria sets.
    assert routes["leaf_story"] == _GOLDEN_LEAF, sorted(routes["leaf_story"] ^ _GOLDEN_LEAF)
    assert routes["overlay_off"] == _GOLDEN_OVERLAY_OFF, sorted(
        routes["overlay_off"] ^ _GOLDEN_OVERLAY_OFF
    )

    # (4) Container criteria (G3/G4) route ONLY where there are children — present for the
    # epic, absent for a leaf story (a second axis of routing diversity).
    assert {"G3", "G4"} <= routes["container_epic"], sorted(routes["container_epic"])
    assert not ({"G3", "G4"} & routes["leaf_story"]), sorted(routes["leaf_story"])


# ── budget shed: the cfg-sensitive `shed_to_budget` path sheds IDENTICALLY on both paths ──
def test_budget_shed_parity(monkeypatch):
    """The corpus tickets never trip `sizing.shed_to_budget` (the cap is never exceeded), and
    the two paths construct `cfg` differently (bespoke: the test `LLMConfig`; workflow: the
    `ProductionBatchRunner`'s `LLMConfig.from_env()` then model override). The `budget_shed`
    scenario forces a shed (a tiny `REBAR_PLAN_REVIEW_BUDGET`); assert BOTH differently-built
    paths shed the SAME criteria set on that cfg-sensitive branch."""
    scn = next(s for s in _CORPUS if s.name == "budget_shed")

    _, b_verdict = _run_bespoke(scn, monkeypatch)
    _, _, w_rec = _run_workflow(scn, monkeypatch)

    b_shed = frozenset((b_verdict["coverage"].get("budget") or {}).get("shed") or [])
    batch_plan = _workflow_batch_plan(w_rec)
    assert batch_plan is not None, "workflow finders step did not journal a batch_plan"
    w_shed = frozenset((batch_plan.get("budget") or {}).get("shed") or [])

    assert b_shed, f"the budget_shed scenario did not actually shed anything (bespoke): {b_verdict}"
    assert b_shed == w_shed, (
        f"budget shed diverged between paths\n"
        f"  bespoke ={sorted(b_shed)}\n  workflow={sorted(w_shed)}"
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
        "budget_shed",
        "missing_ac",
        "child_cycle",
        "oversize_p8",
        "bug_exempt",
    }
    assert required <= set(kinds), required - set(kinds)
    assert "parity" in kinds.values()
    assert "block_shared" in kinds.values()
    # B5 reconciled the P1/P5 divergence — those scenarios are now full-parity, so the
    # `block_divergent` kind no longer exists in the corpus.
    assert "block_divergent" not in kinds.values()
