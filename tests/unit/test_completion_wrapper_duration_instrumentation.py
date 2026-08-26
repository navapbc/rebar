"""Direct, allocation-only timing for the completion close wrapper."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebar import llm
from rebar._commands import close_autoresume
from rebar._snapshot import SOURCE_ATTESTED, SnapshotHandle
from rebar.llm import completion, completion_reconcile, gate_source, review_kernel
from rebar.llm import runner as runner_module
from rebar.llm.config import LLMConfig
from rebar.llm.workflow import (
    completion_criteria,
    completion_verdict_cache,
    executor,
    gate_dispatch,
)

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds * 1_000_000


def test_auto_resume_aggregates_direct_phases_and_preserves_final_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    calls: list[dict[str, Any]] = []
    outcomes = [
        {
            "verdict": "FAIL",
            "findings": [{"criterion": "AC-2"}],
            "criteria": [{"met": False, "evidence_sufficient": False}],
            "metrics": {"requests": 2, "tool_calls": 3, "total_ms": 4.5},
        },
        {
            "verdict": "PASS",
            "findings": [],
            "criteria": [{"met": True, "seeded": True}],
            "metrics": {"requests": 7, "tool_calls": 11, "total_ms": 13_000},
        },
    ]

    def reusable(*_args: Any, **_kwargs: Any) -> None:
        clock.advance_ms(2)
        return None

    def max_resumes(_cfg_root: str | None) -> int:
        clock.advance_ms(3)
        return 1

    def verify_completion(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        phase_metrics = kwargs.get("phase_metrics")
        calls.append(dict(kwargs))
        if isinstance(phase_metrics, dict):
            additions = {
                "verifier_attempt_setup_ms": 1,
                "verifier_handle_resolution_ms": 2,
                "verifier_snapshot_enter_ms": 1,
                "verifier_handle_apply_ms": 1,
                "verifier_inner_setup_ms": 1,
                "verifier_dispatch_ms": 3,
                "verifier_annotation_ms": 1,
                "verifier_snapshot_exit_ms": 1,
                "verifier_handle_defaults_ms": 1,
                "verifier_code_snapshot_ms": 1,
                "verifier_build_drift_ms": 0,
                "verifier_ticket_snapshot_ms": 0,
                "verifier_snapshot_gc_ms": 0,
                "verifier_dispatch_setup_ms": 1,
                "verifier_workflow_ms": 2,
                "verifier_precheck_context_ms": 1,
                "verifier_completion_agent_ms": 1,
                "verifier_verdict_reconcile_ms": 0,
                "verifier_no_llm_passthrough_ms": 0,
                "verifier_unclassified_workflow_steps_ms": 0,
                "verifier_workflow_residual_ms": 0,
                "verifier_dispatch_finalization_ms": 0,
                "verifier_workflow_step_count": 2,
            }
            for key, value in additions.items():
                phase_metrics[key] = phase_metrics.get(key, 0) + value
        clock.advance_ms(10)
        return outcomes.pop(0)

    monkeypatch.setattr(close_autoresume, "monotonic_ns", clock.monotonic_ns, raising=False)
    monkeypatch.setattr(close_autoresume, "_reusable_attested_pass", reusable)
    monkeypatch.setattr(close_autoresume, "_max_resumes", max_resumes)
    monkeypatch.setattr(llm, "verify_completion", verify_completion)
    monkeypatch.setattr(completion_reconcile, "_insufficiency_only", lambda result: True)

    result = close_autoresume.verify_with_auto_resume(
        "T-1", ref=None, repo_root="/repo", cfg_root="/repo"
    )

    assert {
        "verdict": result["verdict"],
        "metrics": result["metrics"],
        "trail": result[close_autoresume.TRAIL_KEY],
        "collector_was_shared": len({id(call.get("phase_metrics")) for call in calls}) == 1,
    } == {
        "verdict": "PASS",
        "metrics": {
            "requests": 7,
            "tool_calls": 11,
            "total_ms": 13_000,
            "verifier_attempt_setup_ms": 2,
            "verifier_handle_resolution_ms": 4,
            "verifier_snapshot_enter_ms": 2,
            "verifier_handle_apply_ms": 2,
            "verifier_inner_setup_ms": 2,
            "verifier_dispatch_ms": 6,
            "verifier_annotation_ms": 2,
            "verifier_snapshot_exit_ms": 2,
            "verifier_handle_defaults_ms": 2,
            "verifier_code_snapshot_ms": 2,
            "verifier_build_drift_ms": 0,
            "verifier_ticket_snapshot_ms": 0,
            "verifier_snapshot_gc_ms": 0,
            "verifier_dispatch_setup_ms": 2,
            "verifier_workflow_ms": 4,
            "verifier_precheck_context_ms": 2,
            "verifier_completion_agent_ms": 2,
            "verifier_verdict_reconcile_ms": 0,
            "verifier_no_llm_passthrough_ms": 0,
            "verifier_unclassified_workflow_steps_ms": 0,
            "verifier_workflow_residual_ms": 0,
            "verifier_dispatch_finalization_ms": 0,
            "verifier_workflow_step_count": 4,
            "verifier_wrapper_setup_ms": 0,
            "verifier_reusable_lookup_ms": 2,
            "verifier_resume_config_ms": 3,
            "verifier_attempts_ms": 20,
            "verifier_between_attempts_ms": 0,
            "verifier_wrapper_finalization_ms": 0,
            "verifier_wrapper_total_ms": 25,
            "verifier_attempt_count": 2,
            "verifier_resume_count": 1,
        },
        "trail": [
            {"attempt": 1, "cache_credited": 0, "remaining_unmet": 1},
            {"attempt": 2, "cache_credited": 1, "remaining_unmet": 0},
        ],
        "collector_was_shared": True,
    }


def test_auto_resume_preserves_single_attempt_result_without_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    verdict = {"verdict": "PASS", "findings": []}

    monkeypatch.setattr(close_autoresume, "monotonic_ns", clock.monotonic_ns, raising=False)
    monkeypatch.setattr(close_autoresume, "_reusable_attested_pass", lambda *_a, **_k: None)
    monkeypatch.setattr(close_autoresume, "_max_resumes", lambda _cfg_root: 1)
    monkeypatch.setattr(llm, "verify_completion", lambda *_a, **_k: verdict)

    result = close_autoresume.verify_with_auto_resume(
        "T-1", ref=None, repo_root="/repo", cfg_root="/repo"
    )

    assert result == verdict
    assert "metrics" not in result


def test_verify_completion_partitions_attempt_without_changing_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    phase_metrics: dict[str, int] = {}
    handle = SnapshotHandle(
        path=Path("/snapshot"), sha="a" * 40, source=SOURCE_ATTESTED, tickets_path="/tickets"
    )
    seen: dict[str, Any] = {}

    def resolve(*_args: Any, **kwargs: Any) -> SnapshotHandle:
        seen["resolve_metrics"] = kwargs.get("phase_metrics")
        clock.advance_ms(2)
        return handle

    @contextlib.contextmanager
    def read_root(_handle: SnapshotHandle, *, phase_metrics: dict[str, int] | None = None):
        seen["context_metrics"] = phase_metrics
        if phase_metrics is not None:
            phase_metrics["verifier_snapshot_enter_ms"] = 1
        clock.advance_ms(1)
        try:
            yield
        finally:
            if phase_metrics is not None:
                phase_metrics["verifier_snapshot_exit_ms"] = 1
            clock.advance_ms(1)

    def apply(config: object, _handle: SnapshotHandle) -> object:
        clock.advance_ms(1)
        return config

    def inner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["inner_metrics"] = kwargs.get("phase_metrics")
        if isinstance(metrics := kwargs.get("phase_metrics"), dict):
            metrics["verifier_inner_setup_ms"] = metrics.get("verifier_inner_setup_ms", 0) + 2
            metrics["verifier_dispatch_ms"] = metrics.get("verifier_dispatch_ms", 0) + 3
        clock.advance_ms(5)
        return {"verdict": "PASS", "findings": [], "metrics": {"requests": 7}}

    def annotate(result: dict[str, Any], _handle: SnapshotHandle) -> dict[str, Any]:
        seen["annotate_input"] = result
        clock.advance_ms(1)
        return result | {"source": "attested"}

    monkeypatch.setattr(completion, "monotonic_ns", clock.monotonic_ns, raising=False)
    monkeypatch.setattr(gate_source, "resolve_gate_handle", resolve)
    monkeypatch.setattr(gate_source, "gate_read_root", read_root)
    monkeypatch.setattr(gate_source, "apply_handle", apply)
    monkeypatch.setattr(gate_source, "annotate_result", annotate)
    monkeypatch.setattr(completion, "_verify_completion_inner", inner)

    config = object()
    result = completion.verify_completion(
        "T-1", repo_root="/repo", config=config, phase_metrics=phase_metrics
    )

    assert {
        "result": result,
        "same_collector": {
            id(seen["resolve_metrics"]),
            id(seen["context_metrics"]),
            id(seen["inner_metrics"]),
        }
        == {id(phase_metrics)},
        "metrics": phase_metrics,
        "annotation_saw_inner_result": seen["annotate_input"]["metrics"] == {"requests": 7},
    } == {
        "result": {
            "verdict": "PASS",
            "findings": [],
            "metrics": {"requests": 7},
            "source": "attested",
        },
        "same_collector": True,
        "metrics": {
            "verifier_attempt_setup_ms": 0,
            "verifier_handle_resolution_ms": 2,
            "verifier_snapshot_enter_ms": 1,
            "verifier_handle_apply_ms": 1,
            "verifier_inner_setup_ms": 2,
            "verifier_dispatch_ms": 3,
            "verifier_annotation_ms": 1,
            "verifier_snapshot_exit_ms": 1,
        },
        "annotation_saw_inner_result": True,
    }


def test_gate_source_attributes_attested_work_and_keeps_local_work_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    calls: list[str] = []
    attested = SnapshotHandle(path=Path("/snapshot"), sha="a" * 40, source=SOURCE_ATTESTED)
    local = SnapshotHandle(path=Path("/repo"), sha=None, source="local")

    def default_ref(_repo_root: str | None) -> str:
        calls.append("default_ref")
        clock.advance_ms(1)
        return "HEAD"

    def default_source(_repo_root: str | None) -> str:
        calls.append("default_source")
        clock.advance_ms(1)
        return SOURCE_ATTESTED

    def acquire(
        _ref: str, *, source_mode: str, repo_root: str | None, fetch: bool
    ) -> SnapshotHandle:
        calls.append(f"acquire:{source_mode}")
        clock.advance_ms(3)
        return attested if source_mode == SOURCE_ATTESTED else local

    def warn_if_behind(_sha: str | None, _repo_root: str | None) -> None:
        calls.append("build_drift")
        clock.advance_ms(4)

    def materialize_tickets(*, repo_root: str | None, fetch: bool) -> str:
        calls.append("materialize_tickets")
        clock.advance_ms(5)
        return "/tickets"

    def maybe_gc(_repo_root: str | None) -> None:
        calls.append("maybe_gc")
        clock.advance_ms(6)

    @contextlib.contextmanager
    def timed_context(name: str, milliseconds: int):
        calls.append(f"enter:{name}")
        clock.advance_ms(milliseconds)
        try:
            yield
        finally:
            calls.append(f"exit:{name}")
            clock.advance_ms(milliseconds)

    monkeypatch.setattr(gate_source, "monotonic_ns", clock.monotonic_ns, raising=False)
    monkeypatch.setattr(gate_source, "default_ref", default_ref)
    monkeypatch.setattr(gate_source, "default_source", default_source)
    monkeypatch.setattr(gate_source, "acquire", acquire)
    monkeypatch.setattr(gate_source.build_drift, "warn_if_behind", warn_if_behind)
    monkeypatch.setattr(gate_source, "materialize_tickets", materialize_tickets)
    monkeypatch.setattr(gate_source.gc_trigger, "maybe_gc", maybe_gc)
    monkeypatch.setattr(gate_source, "gate_session", lambda: timed_context("session", 1))
    monkeypatch.setattr(gate_source, "use_code_root", lambda _root: timed_context("code_root", 2))
    monkeypatch.setattr(
        gate_source, "use_tickets_root", lambda _root: timed_context("tickets_root", 3)
    )

    attested_metrics: dict[str, int] = {}
    attested_result = gate_source.resolve_gate_handle(
        None, None, "/repo", fetch=False, phase_metrics=attested_metrics
    )
    with gate_source.gate_read_root(attested_result, phase_metrics=attested_metrics):
        clock.advance_ms(7)

    local_metrics: dict[str, int] = {}
    local_result = gate_source.resolve_gate_handle(
        "HEAD", "local", "/repo", fetch=False, phase_metrics=local_metrics
    )
    with gate_source.gate_read_root(local_result, phase_metrics=local_metrics):
        clock.advance_ms(7)

    assert {
        "attested_tickets_path": attested_result.tickets_path,
        "attested_metrics": attested_metrics,
        "local_tickets_path": local_result.tickets_path,
        "local_metrics": local_metrics,
        "calls": calls,
    } == {
        "attested_tickets_path": "/tickets",
        "attested_metrics": {
            "verifier_handle_defaults_ms": 2,
            "verifier_code_snapshot_ms": 3,
            "verifier_build_drift_ms": 4,
            "verifier_ticket_snapshot_ms": 5,
            "verifier_snapshot_gc_ms": 6,
            "verifier_snapshot_enter_ms": 6,
            "verifier_snapshot_exit_ms": 6,
        },
        "local_tickets_path": None,
        "local_metrics": {
            "verifier_handle_defaults_ms": 0,
            "verifier_code_snapshot_ms": 3,
            "verifier_build_drift_ms": 4,
            "verifier_ticket_snapshot_ms": 0,
            "verifier_snapshot_gc_ms": 0,
            "verifier_snapshot_enter_ms": 1,
            "verifier_snapshot_exit_ms": 1,
        },
        "calls": [
            "default_ref",
            "default_source",
            "acquire:attested",
            "build_drift",
            "materialize_tickets",
            "maybe_gc",
            "enter:session",
            "enter:code_root",
            "enter:tickets_root",
            "exit:tickets_root",
            "exit:code_root",
            "exit:session",
            "acquire:local",
            "build_drift",
            "enter:session",
            "exit:session",
        ],
    }


def test_completion_inner_separates_setup_from_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rebar import _reads
    from rebar import config as root_config

    clock = _Clock()
    phase_metrics: dict[str, int] = {}
    seen: dict[str, Any] = {}

    def model_for_completion(_repo_path: Path) -> str:
        clock.advance_ms(1)
        return "test-model"

    def max_output_cfg(cfg: LLMConfig) -> LLMConfig:
        clock.advance_ms(1)
        return cfg

    def show_ticket(_ticket_id: str, *, repo_root: str | None) -> dict[str, Any]:
        clock.advance_ms(2)
        return {"ticket_id": "T-1", "ticket_type": "task"}

    def compose_config(_repo_root: str | None) -> SimpleNamespace:
        clock.advance_ms(3)
        return SimpleNamespace(verify=object())

    def criteria(_ticket: dict[str, Any]) -> list[str]:
        clock.advance_ms(2)
        return ["AC-1"]

    def child_count(_ticket_id: str, _repo_root: str | None) -> int:
        clock.advance_ms(1)
        return 0

    def step_floor(_criteria_count: int, _verify_cfg: object, *, direct_children: int) -> int:
        clock.advance_ms(1)
        return 10

    def dispatch(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["dispatch_metrics"] = kwargs.get("phase_metrics")
        if isinstance(metrics := kwargs.get("phase_metrics"), dict):
            metrics["verifier_dispatch_setup_ms"] = 2
            metrics["verifier_workflow_ms"] = 4
            metrics["verifier_precheck_context_ms"] = 1
            metrics["verifier_completion_agent_ms"] = 2
            metrics["verifier_verdict_reconcile_ms"] = 0
            metrics["verifier_no_llm_passthrough_ms"] = 0
            metrics["verifier_unclassified_workflow_steps_ms"] = 0
            metrics["verifier_workflow_residual_ms"] = 1
            metrics["verifier_dispatch_finalization_ms"] = 1
            metrics["verifier_workflow_step_count"] = 3
        clock.advance_ms(7)
        return {"verdict": "PASS", "findings": [], "metrics": {"requests": 5}}

    monkeypatch.setattr(completion, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(completion, "_verifier_model_for_completion", model_for_completion)
    monkeypatch.setattr(review_kernel, "max_output_cfg", max_output_cfg)
    monkeypatch.setattr(_reads, "show_ticket", show_ticket)
    monkeypatch.setattr(root_config, "compose_config", compose_config)
    monkeypatch.setattr(completion_criteria, "explicit_completion_criteria", criteria)
    monkeypatch.setattr(completion_verdict_cache, "direct_child_count", child_count)
    monkeypatch.setattr(completion, "verify_step_floor", step_floor)
    monkeypatch.setattr(gate_dispatch, "produce_completion_verdict", dispatch)

    cfg = LLMConfig.from_env(repo_root=str(tmp_path))
    result = completion._verify_completion_inner(
        "T-1",
        graph=False,
        repo_root=str(tmp_path),
        config=cfg,
        runner=None,
        phase_metrics=phase_metrics,
    )

    assert {
        "result": result,
        "same_collector": seen["dispatch_metrics"] is phase_metrics,
        "metrics": phase_metrics,
    } == {
        "result": {"verdict": "PASS", "findings": [], "metrics": {"requests": 5}},
        "same_collector": True,
        "metrics": {
            "verifier_inner_setup_ms": 11,
            "verifier_dispatch_ms": 7,
            "verifier_dispatch_setup_ms": 2,
            "verifier_workflow_ms": 4,
            "verifier_precheck_context_ms": 1,
            "verifier_completion_agent_ms": 2,
            "verifier_verdict_reconcile_ms": 0,
            "verifier_no_llm_passthrough_ms": 0,
            "verifier_unclassified_workflow_steps_ms": 0,
            "verifier_workflow_residual_ms": 1,
            "verifier_dispatch_finalization_ms": 1,
            "verifier_workflow_step_count": 3,
        },
    }


def test_completion_dispatch_times_existing_work_and_preserves_consumption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _Clock()
    phase_metrics: dict[str, int] = {}

    class _Runner:
        def preflight(self) -> None:
            clock.advance_ms(2)

    runner = _Runner()

    def get_runner(*_args: Any, **_kwargs: Any) -> _Runner:
        clock.advance_ms(1)
        return runner

    def gate_doc(_name: str, _repo_root: str | None) -> dict[str, Any]:
        clock.advance_ms(3)
        return {"name": "completion-verification"}

    def run_workflow(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        recorder = kwargs["recorder"]
        recorder.steps.extend(
            [
                {
                    "step_id": "precheck",
                    "status": "succeeded",
                    "kind": "operation",
                    "duration_ms": 2,
                },
                {
                    "step_id": "verify",
                    "status": "succeeded",
                    "kind": "agent",
                    "duration_ms": 4,
                    "outputs": {"_usage": {"requests": 5, "tool_calls": 7}},
                },
                {
                    "step_id": "reconcile",
                    "status": "succeeded",
                    "kind": "operation",
                    "duration_ms": 1,
                },
            ]
        )
        clock.advance_ms(10)
        return SimpleNamespace(
            status="succeeded", terminal_output={"verdict": "PASS", "findings": []}
        )

    real_attach = gate_dispatch._attach_completion_metrics

    def attach(verdict: dict[str, Any], recorder: Any, total_ms: float) -> None:
        real_attach(verdict, recorder, total_ms)
        clock.advance_ms(5)

    monkeypatch.setattr(gate_dispatch, "monotonic_ns", clock.monotonic_ns, raising=False)
    monkeypatch.setattr(runner_module, "get_runner", get_runner)
    monkeypatch.setattr(gate_dispatch, "_gate_doc", gate_doc)
    monkeypatch.setattr(executor, "run_workflow", run_workflow)
    monkeypatch.setattr(gate_dispatch, "_attach_completion_metrics", attach)

    cfg = LLMConfig.from_env(repo_root=str(tmp_path))
    result = gate_dispatch.produce_completion_verdict(
        "T-1",
        graph=False,
        repo_root=str(tmp_path),
        cfg=cfg,
        runner=runner,
        phase_metrics=phase_metrics,
    )
    consumption = result["metrics"]

    assert {
        "consumption": {
            "requests": consumption["requests"],
            "tool_calls": consumption["tool_calls"],
            "llm_calls": consumption["llm_calls"],
            "llm_ms": consumption["llm_ms"],
            "det_ms": consumption["det_ms"],
            "total_ms_is_numeric": isinstance(consumption["total_ms"], (int, float)),
        },
        "phases": phase_metrics,
    } == {
        "consumption": {
            "requests": 5,
            "tool_calls": 7,
            "llm_calls": 1,
            "llm_ms": 4,
            "det_ms": 3,
            "total_ms_is_numeric": True,
        },
        "phases": {
            "verifier_dispatch_setup_ms": 6,
            "verifier_workflow_ms": 10,
            "verifier_precheck_context_ms": 2,
            "verifier_completion_agent_ms": 4,
            "verifier_verdict_reconcile_ms": 1,
            "verifier_no_llm_passthrough_ms": 0,
            "verifier_unclassified_workflow_steps_ms": 0,
            "verifier_workflow_residual_ms": 3,
            "verifier_dispatch_finalization_ms": 5,
            "verifier_workflow_step_count": 3,
        },
    }


def test_completion_workflow_partition_exposes_short_circuit_and_unknown_steps() -> None:
    metrics: dict[str, int] = {}

    gate_dispatch.attach_completion_workflow_phases(
        metrics,
        [
            {"step_id": "precheck", "duration_ms": 2.4},
            {"step_id": "passthrough", "duration_ms": 1.1},
            {"step_id": "future-step", "duration_ms": 1.2},
            {"step_id": "decide"},
        ],
        6_000_000,
    )

    assert metrics == {
        "verifier_precheck_context_ms": 2,
        "verifier_completion_agent_ms": 0,
        "verifier_verdict_reconcile_ms": 0,
        "verifier_no_llm_passthrough_ms": 1,
        "verifier_unclassified_workflow_steps_ms": 1,
        "verifier_workflow_residual_ms": 2,
        "verifier_workflow_ms": 6,
    }
