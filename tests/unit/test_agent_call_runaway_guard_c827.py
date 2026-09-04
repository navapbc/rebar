"""c827: the runaway actuator — detection must ACT, bounded on both kill paths.

Held-out oracle for the c827 acceptance criteria: a repeating tool-call cycle is
detected DURING the run (windowed distinct-ratio, window 24, trip <= 0.50), aborted
with a typed error, classified typed by ``interpret_failure`` (never the provider-
outage bucket), and routed into the shipped bounded recovery + tool-free finalizer.
Healthy breadth is never touched; both kill paths (requests AND tool calls) are
bounded; the discriminator is memoised once per run_step and single-sourced from
``usage_log``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import usage_log
from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMRunnerError,
    LLMUnavailableError,
    RunawayToolLoopError,
    UnretryableOutputError,
)
from rebar.llm.runner import PydanticAIRunner, RunRequest
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit


def _cfg(max_iterations: int = 200) -> LLMConfig:
    return replace(
        LLMConfig.from_env(), runner="pydantic_ai", repo_path=".", max_iterations=max_iterations
    )


def _req(cfg: LLMConfig) -> RunRequest:
    return RunRequest(
        system_prompt="x",
        instructions="gather evidence",
        config=cfg,
        mode="text",
        reviewers=[],
    )


def _looping_model(calls_per_request: int, counter: dict) -> FunctionModel:
    """Identical signature forever — the 144x production 1-cycle, at 1..3 calls/request."""

    def loop(messages, info: AgentInfo):
        counter["n"] += 1
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=info.function_tools[0].name, args={"path": "."})
                for _ in range(calls_per_request)
            ]
        )

    return FunctionModel(loop)


def _k_cycle_model(counter: dict) -> FunctionModel:
    """A 4-cycle: adjacent-repeat metric scores 1 (its blind spot); the window ratio sees it."""

    def loop(messages, info: AgentInfo):
        counter["n"] += 1
        variant = counter["n"] % 4
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.function_tools[0].name, args={"path": f"./cycle-{variant}"}
                )
            ]
        )

    return FunctionModel(loop)


def _healthy_model(counter: dict, *, land_after: int) -> FunctionModel:
    """The FULL run's shape: high novelty (~0.83 min window ratio), lands naturally."""

    def gather(messages, info: AgentInfo):
        counter["n"] += 1
        if counter["n"] > land_after:
            return ModelResponse(parts=[TextPart("landed")])
        repeat = counter["n"] % 5 == 0
        args = {"path": "."} if repeat else {"path": f"./unique-{counter['n']}"}
        return ModelResponse(parts=[ToolCallPart(tool_name=info.function_tools[0].name, args=args)])

    return FunctionModel(gather)


# ── the actuator fires on a runaway, DURING the run ────────────────────────────────────


def test_one_cycle_runaway_aborts_before_budget_exhaustion():
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)  # request_limit = 100

    with pytest.raises(RunawayToolLoopError) as excinfo:
        PydanticAIRunner(cfg, model_override=_looping_model(1, counter)).run(_req(cfg))

    assert counter["n"] < 40, (
        f"abort must come from detection (~window 24), not the budget: {counter['n']} requests"
    )
    assert not isinstance(excinfo.value, LLMUnavailableError)
    assert "provider call failed" not in str(excinfo.value)


def test_k_cycle_fires_despite_adjacent_metric_blind_spot():
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(cfg, model_override=_k_cycle_model(counter)).run(_req(cfg))

    assert counter["n"] < 40


def test_typed_error_carries_the_repetition_diagnostic():
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError) as excinfo:
        PydanticAIRunner(cfg, model_override=_looping_model(1, counter)).run(_req(cfg))

    diagnostic = getattr(excinfo.value, "diagnostic", None)
    assert isinstance(diagnostic, dict)
    assert diagnostic.get("distinct_ratio_window") is not None
    assert diagnostic["distinct_ratio_window"] <= usage_log.REPETITION_TRIP_RATIO
    top = diagnostic.get("top_repeated_tool_calls")
    assert top, "the diagnostic must name the repeated signatures"
    assert all(":" in entry["signature"] for entry in top)


def test_runaway_warning_names_the_loop_and_merges_run_counters(caplog):
    """interpret_failure's OWN typed branch: a WARNING that names the runaway (never a
    provider-failure line), and run_shape's run counters merged into the guard's
    raise-time diagnostic rather than replacing it."""
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)

    with caplog.at_level(logging.WARNING, logger="rebar.llm.structured_run"):
        with pytest.raises(RunawayToolLoopError) as excinfo:
            PydanticAIRunner(cfg, model_override=_looping_model(1, counter)).run(_req(cfg))

    runaway_lines = [r.getMessage() for r in caplog.records if "runaway" in r.getMessage()]
    assert runaway_lines, "the typed branch must log a WARNING naming the runaway"
    assert any("distinct" in line for line in runaway_lines), "render via format_repetition"
    assert not any("provider call failed" in r.getMessage().lower() for r in caplog.records), (
        "a runaway must never be reported as a provider failure"
    )
    diagnostic = excinfo.value.diagnostic
    assert diagnostic.get("requests"), "run_shape counters must be merged in"
    assert diagnostic.get("tool_calls"), "raise-time tool-call count must survive the merge"


# ── both kill paths are bounded (requests AND tool calls) ──────────────────────────────


@pytest.mark.parametrize(
    ("calls_per_request", "max_requests"),
    [
        (1, 32),
        (2, 17),
        # The arm a run_step-keyed guard fails: at 3 calls/request the window fills in
        # 8 requests; a guard counting steps would run ~3x longer before acting.
        (3, 12),
    ],
)
def test_runaway_bounded_at_any_calls_per_request(calls_per_request, max_requests):
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)

    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(cfg, model_override=_looping_model(calls_per_request, counter)).run(
            _req(cfg)
        )

    assert counter["n"] <= max_requests, (
        f"at {calls_per_request} calls/request detection must key on TOOL CALLS: "
        f"{counter['n']} requests"
    )


# ── healthy breadth is never touched; no new workload ceiling ──────────────────────────


def test_healthy_full_profile_is_never_aborted():
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)

    result = PydanticAIRunner(cfg, model_override=_healthy_model(counter, land_after=40)).run(
        _req(cfg)
    )

    assert result["text"] == "landed"
    assert counter["n"] == 41, "a high-novelty run must land naturally, never be aborted"


def test_healthy_run_past_flat_landing_threshold_is_never_aborted():
    """No new workload ceiling: a high-novelty run larger than the previous flat-landing
    threshold (239 requests) lands naturally instead of being aborted for size."""
    counter = {"n": 0}
    cfg = _cfg(max_iterations=600)  # request_limit = 300 > 239

    result = PydanticAIRunner(cfg, model_override=_healthy_model(counter, land_after=250)).run(
        _req(cfg)
    )

    assert result["text"] == "landed"
    assert counter["n"] == 251, "the run must land past 239 requests, never be aborted"


def test_trip_threshold_is_read_from_usage_log_at_call_time(monkeypatch):
    """Single-sourcing pin: raising the shared constant makes the healthy profile trip,
    so the guard cannot be reading a private literal."""
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)
    monkeypatch.setattr(usage_log, "REPETITION_TRIP_RATIO", 0.95)

    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(cfg, model_override=_healthy_model(counter, land_after=200)).run(_req(cfg))


def test_window_is_read_from_usage_log_at_call_time(monkeypatch):
    counter = {"n": 0}
    cfg = _cfg(max_iterations=200)
    monkeypatch.setattr(usage_log, "REPETITION_WINDOW", 8)

    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(cfg, model_override=_looping_model(1, counter)).run(_req(cfg))

    assert counter["n"] <= 12, "a smaller shared window must mean earlier detection"


# ── cost: the discriminator is memoised once per run_step ──────────────────────────────


def test_ratio_computed_at_most_once_per_run_step(monkeypatch):
    counter = {"n": 0}
    computations = {"n": 0}
    real = usage_log.window_distinct_ratio

    def counting(*args, **kwargs):
        computations["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(usage_log, "window_distinct_ratio", counting)
    cfg = _cfg(max_iterations=200)

    result = PydanticAIRunner(
        cfg, model_override=_healthy_model_parallel(counter, land_after=10)
    ).run(_req(cfg))

    assert result["text"] == "landed"
    assert computations["n"] <= counter["n"], (
        f"{computations['n']} ratio computations across {counter['n']} run steps — "
        "the discriminator must be memoised on the step number"
    )


def _healthy_model_parallel(counter: dict, *, land_after: int) -> FunctionModel:
    """Three DISTINCT calls per request: unmemoised guards compute 3x per step."""

    def gather(messages, info: AgentInfo):
        counter["n"] += 1
        if counter["n"] > land_after:
            return ModelResponse(parts=[TextPart("landed")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.function_tools[0].name,
                    args={"path": f"./s{counter['n']}-c{i}"},
                )
                for i in range(3)
            ]
        )

    return FunctionModel(gather)


# ── the producer helper is single-sourced beside the constants ─────────────────────────


def test_window_distinct_ratio_matches_repetition_summary():
    signatures = [f"tool:{i % 4:08d}" for i in range(30)]

    assert usage_log.window_distinct_ratio(signatures) == pytest.approx(4 / 24, abs=1e-3)
    assert usage_log.window_distinct_ratio(["a"] * 10) is None, "below the window: no accusation"
    summary = usage_log._repetition_summary(signatures)
    assert summary["distinct_ratio_window"] == usage_log.window_distinct_ratio(signatures)


# ── routing: a runaway lands in the shipped bounded recovery ───────────────────────────


def _ticket() -> dict:
    criteria = "\n".join(f"- [ ] criterion {index}" for index in range(1, 4))
    return {
        "ticket_id": "T-1",
        "title": "runaway routing",
        "ticket_type": "task",
        "description": f"## Acceptance Criteria\n{criteria}",
    }


def _ctx() -> StepContext:
    return StepContext(
        run_id="run-1",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "completion-verifier",
            "mode": "structured",
            "output_schema": "completion_verdict",
        },
        inputs={
            "ticket_id": "T-1",
            "context": "<untrusted_ticket_context>ticket</untrusted_ticket_context>",
        },
        workflow={"name": "completion-verification"},
        target_ticket="T-1",
        repo_root=None,
    )


_RUNAWAY_DIAGNOSTIC = {
    "requests": 24,
    "tool_calls": 260,
    "tool_calls_distinct": 12,
    "max_consecutive_repeat": 1,
    "distinct_ratio_window": 0.167,
    "top_repeated_tool_calls": [{"signature": "search_files:ab12cd34", "count": 64}],
}


def _remainder_ids(instructions: str) -> list[str]:
    """The criterion ids a batched successor is handed, parsed from its user message (the
    ``- <id>: <text>`` lines under the ``Remainder to verify`` header)."""
    ids: list[str] = []
    collecting = False
    for line in instructions.splitlines():
        if line.startswith("Remainder to verify"):
            collecting = True
            continue
        if collecting:
            if not line.strip():
                break
            match = re.match(r"- (\S+):", line)
            if match:
                ids.append(match.group(1))
    return ids


class _RunawayThenRecoverRunner:
    name = "runaway-then-recover"

    def __init__(self) -> None:
        self.requests = []

    def preflight(self) -> None:
        return None

    def run(self, req):
        self.requests.append(req)
        if len(self.requests) == 1:
            err = RunawayToolLoopError("runaway tool-call loop detected")
            err.diagnostic = dict(_RUNAWAY_DIAGNOSTIC)  # type: ignore[attr-defined]
            raise err
        if req.execution_mode == "agentic":
            # A batched recovery successor: bank a met verdict for every remainder criterion
            # it was handed, the way a real successor banks via record_criterion_verdict /
            # its structured `criteria` output (harvested into the bank).
            return {
                "criteria": [
                    {
                        "criterion_id": cid,
                        "met": True,
                        "evidence": "src/example.py:10",
                    }
                    for cid in _remainder_ids(req.instructions)
                ]
            }
        payload = json.loads(req.instructions)
        criteria = [
            {
                "criterion": criterion,
                "met": True,
                "citation": {"kind": "source", "description": "src/example.py:10"},
                "kind": "codebase-verifiable",
            }
            for criterion in payload["expected_criteria"]
        ]
        return {"verdict": "PASS", "findings": [], "criteria": criteria}


class _RunawayThenTruncateRunner(_RunawayThenRecoverRunner):
    def run(self, req):
        if not self.requests:
            return super().run(req)
        self.requests.append(req)
        raise UnretryableOutputError("finish_reason=length")


def test_runaway_routes_into_bounded_recovery(monkeypatch) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _RunawayThenRecoverRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))

    result = step.run(_ctx())

    assert result.status == "succeeded"
    assert result.outputs["verdict"] == "PASS"
    evidence_runs = [r for r in runner.requests[1:] if r.execution_mode == "agentic"]
    assert evidence_runs, "recovery must gather evidence in FRESH successor runs"
    assert all(r.iteration_limit and r.iteration_limit > 0 for r in evidence_runs), (
        "each recovery successor must carry a bounded iteration budget"
    )


def test_runaway_double_failure_sidecar_names_the_runaway(monkeypatch) -> None:
    from rebar.llm.workflow.completion_recovery import raise_completion_workflow_failure
    from rebar.llm.workflow.executor import RunResult

    monkeypatch.setattr("rebar._reads.show_ticket", lambda *args, **kwargs: _ticket())
    runner = _RunawayThenTruncateRunner()
    step = CompletionAgentStep(runner=runner, repo_root=None, config=LLMConfig(runner="fake"))
    with pytest.raises(CompletionRecoveryError):
        step.run(_ctx())

    emitted: list[dict] = []
    monkeypatch.setattr(
        "rebar.llm.gate_error_sidecar.emit_gate_error",
        lambda *args, **kwargs: emitted.append(kwargs) or True,
    )
    result = RunResult(
        run_id="run-1",
        workflow_name="completion-verification",
        status="failed",
        outputs={},
        terminal_step="verify",
        terminal_output=None,
        error="completion verification bounded recovery failed",
    )
    with pytest.raises(CompletionRecoveryError) as excinfo:
        raise_completion_workflow_failure("T-1", result, step.failure_diagnostic, 1, None)

    assert emitted, "the terminal state must never be a bare LLMError with no sidecar"
    diagnostic = emitted[0]["diagnostic"]
    assert diagnostic["aggregate_distinct_ratio_window"] == 0.167
    assert diagnostic["aggregate_top_repeated_tool_calls"] == [
        {"signature": "search_files:ab12cd34", "count": 64}
    ]
    assert "max_consecutive_repeat=1" in str(excinfo.value)


def test_runaway_error_is_a_typed_runner_error():
    assert issubclass(RunawayToolLoopError, LLMRunnerError)
    assert not issubclass(RunawayToolLoopError, LLMUnavailableError)
