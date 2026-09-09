"""Require Pass-1 thread fan-out to preserve the active gate session.

Raw ``ThreadPoolExecutor`` workers do not inherit ``ContextVar`` state. Without explicit
propagation, agentic calls fail ``assert_gated`` before any LLM call and their findings are
dropped. An offline runner reproduces that boundary.
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.gate_context import assert_gated, gate_session, in_gate_session
from rebar.llm.plan_review import pass1
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner


class GateCheckingRunner(FakeRunner):
    """Record whether an agentic call sees the gate before its first model action."""

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
    """Every container fan-out worker must observe the caller's active gate session."""
    ctx = _ctx()
    cfg = LLMConfig.from_env(repo_root=str(tmp_path))
    runner = GateCheckingRunner([_finding("G3", "c1")])
    coverage: dict = {}

    with gate_session():
        out, _calls = pass1._run_container(ctx, cfg, runner, _CRITERIA, coverage)

    agentic_seen = [seen for mode, seen in runner.calls if mode != "single_turn"]
    assert agentic_seen, "expected at least one agentic container pairing call"
    assert all(agentic_seen), (
        "container fan-out workers lost the gate session (ContextVar not propagated to threads)"
    )
    assert out, "container findings were dropped (assert_gated fired in a worker thread)"
