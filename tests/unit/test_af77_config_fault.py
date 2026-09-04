from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.criteria import CriteriaError
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit


def _ctx(*, description: str | None = None) -> PlanContext:
    return PlanContext(
        ticket_id="T-af77",
        ticket_type="story",
        title="Configure criteria",
        description=description
        or (
            "## Why\nKeep plan review deterministic.\n\n"
            "## What\nLoad project criteria.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] criteria configuration failures are reported (`pytest -q`)\n"
        ),
    )


def _cfg(tmp_path) -> LLMConfig:
    return dataclasses.replace(
        LLMConfig(runner="fake"), model="claude-haiku-4-5", repo_path=str(tmp_path)
    )


def _run_assemble_failure(monkeypatch, tmp_path, exc: Exception, *, ctx: PlanContext | None = None):
    from rebar.llm.plan_review import context_assembly, orchestrator, production_batch_runner
    from rebar.llm.workflow import gate_dispatch, recorder

    # Preload the batch runner before installing the fault. It binds route_criteria at module
    # import, so a first import under the patch would leak the injected exception into later tests.
    assert production_batch_runner.route_criteria is orchestrator.route_criteria
    ctx = ctx or _ctx()
    rec = recorder.MemoryRecorder()
    monkeypatch.setattr(recorder, "MemoryRecorder", lambda: rec)
    monkeypatch.setattr(context_assembly, "assemble_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(
        orchestrator,
        "route_criteria",
        lambda *args, **kwargs: (_ for _ in ()).throw(exc),
    )
    verdict = gate_dispatch.produce_plan_review_verdict(
        ctx,
        _cfg(tmp_path),
        runner=FakeRunner(),
        advisory_cap=10,
        repo_root=None,
    )
    return ctx, rec, verdict


def test_criteria_error_is_a_config_fault_not_an_llm_outage(monkeypatch, tmp_path) -> None:
    from rebar.llm.workflow import plan_review_recovery

    sentinel = "malformed-overlay sentinel"
    ctx, rec, verdict = _run_assemble_failure(monkeypatch, tmp_path, CriteriaError(sentinel))

    failed = next(
        step
        for step in rec.steps
        if step["frame_key"] == "review@then/assemble" and step["status"] == "failed"
    )
    assert failed["status"] == "failed"
    assert failed["kind"] == "scripted"
    assert failed["outputs"]["failure_kind"] == "criteria_config"
    assert failed["error"] == sentinel
    assert not [
        step
        for step in rec.steps
        if step.get("kind") in {"agent", "batch"} and step.get("status") == "succeeded"
    ]

    coverage = verdict["coverage"]
    assert verdict["verdict"] == "INDETERMINATE"
    assert coverage["llm_ran"] is False
    assert coverage["config_fault"] is True
    assert "llm_unavailable" not in coverage
    assert sentinel in coverage["config_error"]
    assert "installed rebar build" in coverage["config_error"]
    assert "repository checkout" in coverage["config_error"]

    degraded = plan_review_recovery._degraded_plan_review_verdict(
        ctx,
        _cfg(tmp_path),
        error=CriteriaError(sentinel),
        advisory_cap=10,
        runner_name="fake",
    )
    assert coverage["det"] == degraded["coverage"]["det"]
    assert coverage["counts"] == degraded["coverage"]["counts"]


def test_unrelated_assemble_runtime_error_keeps_generic_degradation(monkeypatch, tmp_path) -> None:
    _ctx_value, rec, verdict = _run_assemble_failure(
        monkeypatch, tmp_path, RuntimeError("unrelated assemble failure")
    )

    failed = next(
        step
        for step in rec.steps
        if step["frame_key"] == "review@then/assemble" and step["status"] == "failed"
    )
    assert failed["outputs"] == {}
    assert verdict["coverage"]["llm_unavailable"] is True
    assert "config_fault" not in verdict["coverage"]


def test_genuine_preflight_outage_is_unchanged(monkeypatch, tmp_path) -> None:
    from rebar.llm.workflow import gate_dispatch

    class OutageRunner(FakeRunner):
        def preflight(self) -> None:
            raise LLMUnavailableError("genuine outage")

    monkeypatch.setattr(gate_dispatch, "emit_gate_error", lambda *args, **kwargs: None)
    verdict = gate_dispatch.produce_plan_review_verdict(
        _ctx(), _cfg(tmp_path), runner=OutageRunner(), advisory_cap=10, repo_root=None
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["coverage"]["llm_unavailable"] is True
    assert verdict["coverage"]["llm_ran"] is False
    assert "config_fault" not in verdict["coverage"]


def test_config_fault_preserves_deterministic_block(monkeypatch, tmp_path) -> None:
    from rebar.llm.plan_review import det_floor

    ctx = _ctx(description="The plan has no acceptance-criteria checklist.")
    real_run_det_floor = det_floor.run_det_floor
    det_runs = iter([real_run_det_floor(_ctx()), real_run_det_floor(ctx)])
    monkeypatch.setattr(det_floor, "run_det_floor", lambda _ctx_value: next(det_runs))
    _ctx_value, _rec, verdict = _run_assemble_failure(
        monkeypatch,
        tmp_path,
        CriteriaError("malformed-overlay sentinel"),
        ctx=ctx,
    )
    assert verdict["coverage"]["config_fault"] is True
    assert verdict["blocking"]
    assert verdict["verdict"] == "BLOCK"
