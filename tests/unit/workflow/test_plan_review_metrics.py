"""Per-pass latency/cost telemetry on the WORKFLOW plan-review gate (toy-kink-ire).

B-RETIRE removed bespoke ``run_review`` — the only producer of ``coverage['metrics']``
(db7b AC5: det_ms / llm_ms / total_ms / llm_calls / claim_path) — so the workflow gate
emitted an empty sidecar ``metrics``. These tests prove the metrics are reinstated on the
workflow path: the interpreter records per-step ``duration_ms``, ``gate_dispatch`` reconstructs
the tier split + a cost proxy into ``coverage['metrics']``, and the sidecar lifts it again.

Driven fully OFFLINE: a single branching FakeRunner returns schema-valid (empty) payloads for
the finder / verify / coach steps, so the whole pipeline succeeds without a model.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm import findings as _findings
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_TARGET = "T-1"
_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)


def _patch_reads(monkeypatch) -> None:
    import rebar

    state = {
        "ticket_id": _TARGET,
        "ticket_type": "story",
        "title": "Build X",
        "description": _GOOD_AC,
        "deps": [],
    }
    monkeypatch.setattr(rebar, "show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr(rebar, "list_tickets", lambda parent=None, repo_root=None: [])


class _BranchingRunner(FakeRunner):
    """One runner for ALL plan-review LLM steps (the gate wires a single runner into both the
    finder batch and the verify/coach agent steps). Branches on the step's output schema and
    returns a schema-valid EMPTY payload, so the run reaches a clean PASS with no model."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, req):
        self.calls += 1
        schema = req.output_schema or ""
        if "verification" in schema:
            payload: dict = {"verifications": []}
        elif "coach" in schema:
            payload = {"notes": [{"move_id": "1", "subject": "the X design", "finding_refs": []}]}
        else:  # the Pass-1 finder
            payload = {"analysis": "", "findings": []}
        validated = _findings.validate_structured(dict(payload), schema)
        return {**validated, "runner": self.name, "model": None, "trace_id": None}


def _run_gate(monkeypatch):
    _patch_reads(monkeypatch)
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-haiku-4-5")
    ctx = PlanContext(ticket_id=_TARGET, ticket_type="story", title="Build X", description=_GOOD_AC)
    runner = _BranchingRunner()
    verdict = gate_dispatch.produce_plan_review_verdict(
        ctx, cfg, runner=runner, advisory_cap=10, repo_root=None
    )
    return verdict, runner


def test_workflow_gate_emits_coverage_metrics(monkeypatch) -> None:
    """The workflow plan-review path emits coverage['metrics'] with the db7b fields, captured
    from the run's recorder step timings + the finder criteria count."""
    verdict, runner = _run_gate(monkeypatch)

    assert verdict["verdict"] == "PASS", verdict.get("coverage")
    assert runner.calls > 0, "the LLM tier must have run (finder + verify + coach)"

    metrics = verdict["coverage"]["metrics"]
    for key in ("det_ms", "llm_ms", "total_ms", "llm_calls", "claim_path"):
        assert key in metrics, f"coverage['metrics'] missing {key!r}: {metrics}"
    # Latency is real wall-clock captured from the workflow run.
    assert metrics["det_ms"] >= 0 and metrics["llm_ms"] >= 0
    assert metrics["total_ms"] >= metrics["llm_ms"], "total spans the whole run incl. the LLM tier"
    # llm_calls is a cost proxy that scales with the work actually done (finder criteria + the
    # verify/coach agent steps) — non-zero on a real LLM-tier run.
    assert metrics["llm_calls"] > 0
    assert "no-llm/no-network" in metrics["claim_path"]


def test_sidecar_lifts_metrics_on_workflow_gate(monkeypatch) -> None:
    """The sidecar carries the metrics again (lifted to the top level for offline join)."""
    from rebar.llm.plan_review import sidecar

    verdict, _ = _run_gate(monkeypatch)
    payload = sidecar.build_payload(verdict, material="x")
    assert payload["metrics"] == verdict["coverage"]["metrics"]
    assert payload["metrics"]["total_ms"] >= 0
