"""The workflow gate path — byte-compatible signing and the faithful degradation
semantics (INDETERMINATE-on-outage for plan-review; fail-closed for completion).

These are the production-path guarantees the workflow gate must keep: the SIGNING is
untouched (so attestations stay byte-compatible across verdict-state-equivalent runs) and
a systemic LLM outage degrades cleanly (never a hollow PASS / silent close).
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.plan_review import attest
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit


# ── byte-compatible signing (the load-bearing guarantee) ────────────────────────────
def _passing_verdict() -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": "T-1",
        "ticket_type": "story",
        "blocking": [],
        "advisory": [{"id": "a1", "finding": "x", "criteria": ["E1"]}],
        "coaching": [],
        "overflow": [],
        "indeterminate": [],
        "dropped": [],
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 1}},
        "runner": "pydantic_ai",
        "model": "claude-opus-4-8",
    }


def test_manifest_is_deterministic_and_path_independent() -> None:
    # The signed manifest is a pure function of the verdict STATE (no timestamps, no
    # path/engine identity) — so the bespoke and workflow paths, producing the same verdict
    # state, sign a byte-identical manifest. Building it twice must be byte-identical.
    v = _passing_verdict()
    m1 = attest.build_manifest(v, material="fp-abc", deps={"src/x.py": "h1"}, regver="rv1")
    m2 = attest.build_manifest(dict(v), material="fp-abc", deps={"src/x.py": "h1"}, regver="rv1")
    assert m1 == m2
    # The manifest pins the verdict + counts + material + deps + regver — NOT coverage
    # internals (which differ between paths). Mutating coverage internals must not change it.
    v2 = dict(v)
    v2["coverage"] = {"counts": {"blocking": 0, "advisory_surfaced": 1}, "llm_ran": True, "x": 9}
    m3 = attest.build_manifest(v2, material="fp-abc", deps={"src/x.py": "h1"}, regver="rv1")
    assert m3 == m1


# ── plan-review INDETERMINATE-on-outage ────────────────────────────────────────────
class _OutageRunner(FakeRunner):
    """A runner whose preflight reports the LLM tier unavailable (a systemic outage)."""

    name = "fake"

    def preflight(self) -> None:
        raise LLMUnavailableError("no agents extra / key")


def _ctx() -> PlanContext:
    return PlanContext(
        ticket_id="T-1",
        ticket_type="story",
        title="Build X",
        description=(
            "## Why\nneed X\n\n## What\nbuild X in src/x.py\n\n## Scope\njust X\n\n"
            "## Acceptance Criteria\n- [ ] X persists\n- [ ] seam calls X\n"
        ),
    )


def test_plan_review_workflow_outage_degrades_to_unsigned_indeterminate() -> None:
    from rebar.llm.workflow import gate_dispatch

    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    verdict = gate_dispatch.produce_plan_review_verdict(
        _ctx(), cfg, runner=_OutageRunner(), advisory_cap=10, repo_root=None
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["coverage"].get("llm_unavailable") is True
    assert verdict["coverage"].get("llm_ran") is False
    # An INDETERMINATE verdict is NEVER a PASS, so review_plan's PASS-only signing gate
    # never signs it (the fuel-posse-ball guard) — assert the verdict shape that drives that.
    assert verdict["verdict"] != "PASS"


# ── plan-review coach failure is NON-fatal ──────────────────────────────────────────
def test_plan_review_coach_failure_recovers_verdict_without_coaching() -> None:
    # Pass-4 coach is advisory polish — the verdict is emitted even when the coach
    # fails. The workflow path reconstructs the verdict from the recorded Pass-3 `decide`
    # partition with EMPTY coaching, rather than degrading a valid PASS to INDETERMINATE.
    from rebar.llm.workflow import gate_dispatch
    from rebar.llm.workflow.recorder import MemoryRecorder

    rec = MemoryRecorder()
    rec.steps = [
        {
            "frame_key": "precheck",
            "status": "succeeded",
            "outputs": {"canonical_id": "T-1", "ticket_type": "story", "det_coverage": {"x": 1}},
        },
        {
            "frame_key": "review@then/assemble",
            "status": "succeeded",
            "outputs": {"routing": {"single_turn": ["E1"], "agent_tier": []}},
        },
        {
            "frame_key": "review@then/verify_branch@else/decide",
            "status": "succeeded",
            "outputs": {
                "blocking": [],
                "surfaced": [{"id": "a1", "finding": "x", "criteria": ["E1"]}],
                "overflow": [],
                "indeterminate": [],
                "dropped": [],
            },
        },
        {
            "frame_key": "review@then/verify_branch@else/coach_notes",
            "status": "failed",
            "outputs": {},
        },
    ]
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    verdict = gate_dispatch._recover_plan_review_coach_failure(rec, cfg, error="coach boom")
    assert verdict is not None
    assert verdict["verdict"] == "PASS"  # no blocking → PASS even though the coach failed
    assert verdict["coaching"] == []  # coaching dropped, not the whole verdict
    assert verdict["advisory"], "the real Pass-1/2 findings survive the coach failure"
    assert verdict["coverage"].get("coach_error")
    assert verdict["coverage"].get("llm_ran") is True  # the LLM tier DID run (not an outage)


def test_plan_review_coach_recovery_returns_none_when_decide_absent() -> None:
    # If Pass-3 `decide` did NOT succeed, the LLM tier genuinely failed (not just the coach) —
    # the recovery declines so the caller degrades to INDETERMINATE.
    from rebar.llm.workflow import gate_dispatch
    from rebar.llm.workflow.recorder import MemoryRecorder

    rec = MemoryRecorder()
    rec.steps = [{"frame_key": "precheck", "status": "succeeded", "outputs": {"canonical_id": "T"}}]
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    assert gate_dispatch._recover_plan_review_coach_failure(rec, cfg, error="x") is None


# ── code-read-root vs ticket-store-root (epic 5ca8 / joe-debug: the no_repo_root class) ─
def test_precheck_grounds_code_root_without_hijacking_ticket_root(monkeypatch, tmp_path) -> None:
    """The det-floor op must keep the two read-roots DISTINCT:

      * the CODE read-root (the active attested snapshot — origin/main HEAD by default) drives
        `PlanContext.repo_root`, so DET check P2 ('resolution') can ground against real code;
      * the TICKET-store read-root drives `assemble_context`'s `rebar.show_ticket(repo_root=…)`
        and stays whatever the workflow threads (the ticket snapshot / live store).

    Before the fix `PlanContext.repo_root` was None and P2 abstained `no_repo_root`. A naive
    fix that threaded the CODE snapshot as the op's `repo_root` then broke ticket reads
    ('Ticket not found'), because the ticket store is NOT under the .git-less code snapshot.
    Both must hold at once."""
    import rebar
    from rebar.llm import config as llm_config
    from rebar.llm.plan_review import workflow_ops
    from rebar.llm.workflow.executor import StepContext

    seen_ticket_root: dict = {}

    def _show(tid, repo_root=None):
        seen_ticket_root["v"] = repo_root
        return {
            "ticket_id": tid,
            "ticket_type": "task",
            "title": "T",
            "description": "## Acceptance Criteria\n- [ ] do the thing\n",
        }

    monkeypatch.setattr(rebar, "show_ticket", _show)
    monkeypatch.setattr(rebar, "list_tickets", lambda parent=None, repo_root=None: [])

    code_snap = str(tmp_path / "code-snapshot")  # the active attested CODE snapshot
    sc = StepContext(
        run_id="r",
        step_id="precheck",
        kind="uses",
        step={},
        inputs={"ticket_id": "T-1"},
        workflow={},
        target_ticket="T-1",
        repo_root=None,  # the TICKET-store read-root the workflow threads (live/ticket store)
    )
    token = llm_config._active_code_root.set(code_snap)
    try:
        out = workflow_ops.plan_review_precheck(sc)
    finally:
        llm_config._active_code_root.reset(token)
    # Ticket reads used the workflow's ticket-store root (None → live/ticket store), NOT the
    # code snapshot — so a real ticket would still resolve.
    assert seen_ticket_root["v"] is None, seen_ticket_root
    # P2 grounded against the active CODE snapshot (ran, not the no_repo_root abstain).
    p2 = out["det_coverage"]["P2"]
    assert p2.get("reason") != "no_repo_root", p2
    assert p2.get("ran") is True, p2


def test_produce_plan_review_does_not_thread_code_snapshot_as_ticket_root(monkeypatch) -> None:
    """`produce_plan_review_verdict` must pass the TICKET-store `repo_root` it was handed
    THROUGH to `run_workflow` unchanged — it must NOT substitute `cfg.repo_path` (the CODE
    snapshot), which would reach `assemble_context`'s ticket reads via StepContext and make
    them look for the store under the .git-less code snapshot ('Ticket not found')."""
    from rebar.llm.runner import FakeRunner
    from rebar.llm.workflow import executor as _ex
    from rebar.llm.workflow import gate_dispatch

    captured: dict = {}

    class _Res:
        status = "succeeded"
        terminal_output = {"verdict": "PASS", "ticket_id": "T-1", "coverage": {}}
        error = None

    def _fake_run_workflow(doc, inputs=None, *, target_ticket=None, repo_root=None, **kw):
        captured["workflow_repo_root"] = repo_root
        return _Res()

    monkeypatch.setattr(_ex, "run_workflow", _fake_run_workflow)
    monkeypatch.setattr(
        gate_dispatch, "_gate_doc", lambda name, repo_root: {"id": "g", "steps": []}
    )

    cfg = dataclasses.replace(
        LLMConfig(runner="fake"), model="claude-haiku-4-5", repo_path="/code/snapshot"
    )
    gate_dispatch.produce_plan_review_verdict(
        _ctx(), cfg, runner=FakeRunner(), advisory_cap=10, repo_root="/ticket/store/root"
    )
    # The ticket-store root is threaded through; the code snapshot is NOT substituted for it.
    assert captured["workflow_repo_root"] == "/ticket/store/root", captured
    assert captured["workflow_repo_root"] != "/code/snapshot", captured


# ── completion fail-closed-on-outage (preflight raises → close gate blocks) ─────────
def test_completion_workflow_outage_raises_so_close_fails_closed(monkeypatch) -> None:
    import rebar
    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(rebar, "show_ticket", lambda tid, repo_root=None: {"ticket_id": tid})
    monkeypatch.setattr(rebar, "list_tickets", lambda parent=None, repo_root=None: [])
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    with pytest.raises(LLMUnavailableError):
        gate_dispatch.produce_completion_verdict(
            "T-1", graph=False, repo_root=None, cfg=cfg, runner=_OutageRunner()
        )
