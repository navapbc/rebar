"""Regression: the Pass-1 THREAD fan-out must preserve the active gate session.

Root cause (joe-debug): ``_in_gate_session`` is a ``contextvars.ContextVar`` set in the
main thread by ``gate_read_root``/``gate_session``. The plan-review Pass-1 fan-out dispatches
agentic ``runner.run`` calls to a raw ``ThreadPoolExecutor`` — and a ContextVar is inherited
by asyncio tasks but NOT by raw threads (documented at ``llm/config.py``). So in the worker
the session reads ``False``, ``runner.run`` (agentic) calls ``assert_gated('agentic filesystem
tools')`` (``runner.py``) which raises ``RuntimeError`` at ~0.0s — BEFORE any LLM call. The
container path logs it as ``container bin ... FAILED ... (RuntimeError)`` and drops the
pairing's findings; the chunk path's ladder swallows it (``return []``). Net: every agentic
Pass-1 call in the fan-out is silently dropped while the gate still returns PASS.

These tests exercise the real mechanism with NO live LLM by injecting a runner that
replicates the real ``PydanticAIRunner.run``'s first agentic action (``assert_gated``).
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig, assert_gated, gate_session, in_gate_session
from rebar.llm.plan_review import pass1
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner


class GateCheckingRunner(FakeRunner):
    """Replicates the FIRST action the real ``PydanticAIRunner.run`` takes on an *agentic*
    call — ``assert_gated('agentic filesystem tools')`` — and records, per call, the
    execution mode and whether the gate session was visible. This lets a no-LLM test
    reproduce the worker-thread gate-session loss that drops agentic findings."""

    def __init__(self, findings: list[dict]):
        super().__init__([])
        self._findings = findings
        self.calls: list[tuple[str, bool]] = []

    def run(self, req):  # type: ignore[override]
        self.calls.append((req.execution_mode, in_gate_session()))
        if req.execution_mode != "single_turn":
            # Exactly what runner.py does before any model call on an agentic run.
            assert_gated("agentic filesystem tools")
        return {"findings": [dict(f) for f in self._findings]}


def _finding(cid: str, child: str) -> dict:
    return {
        "finding": "demo finding",
        "criteria": [cid],
        "location": f"child {child}",
        "evidence": [],
        "scenarios": [],
        "impact": "",
        "checklist_item": "",
    }


def _ctx() -> PlanContext:
    return PlanContext(
        ticket_id="parent",
        ticket_type="epic",
        title="Parent epic under review",
        description="A parent plan with two small children to pack into one container bin.",
        children=[
            {"ticket_id": "c1", "title": "Child one", "description": "child one body text"},
            {"ticket_id": "c2", "title": "Child two", "description": "child two body text"},
        ],
    )


_CRITERIA = [
    {"id": "G3", "name": "decomposition", "scenario": "Are the children well-formed?"},
    {"id": "G4", "name": "coverage", "scenario": "Do the children cover the parent?"},
]


def test_container_fanout_preserves_gate_session(tmp_path):
    """The container fan-out (``_run_container``) runs agentic pairings in a ThreadPoolExecutor.
    Inside an active gate session, those workers MUST still observe it — otherwise assert_gated
    raises in the worker, the pairing's findings are dropped, and the agentic container tier
    silently produces nothing."""
    ctx = _ctx()
    cfg = LLMConfig.from_env(repo_root=str(tmp_path))
    runner = GateCheckingRunner([_finding("G3", "c1")])
    coverage: dict = {}

    with gate_session():
        out = pass1._run_container(ctx, cfg, runner, _CRITERIA, coverage)

    agentic_seen = [seen for mode, seen in runner.calls if mode != "single_turn"]
    assert agentic_seen, "expected at least one agentic container pairing call"
    assert all(agentic_seen), (
        "container fan-out workers lost the gate session (ContextVar not propagated to threads)"
    )
    assert out, "container findings were dropped (assert_gated fired in a worker thread)"
