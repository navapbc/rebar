"""Unit tests for the pure scripted steps (WS-E): gate (versioned policy) +
render_context. Store-touching steps are pinned in the interface tier."""

from __future__ import annotations

from rebar.llm.workflow import steps
from rebar.llm.workflow.executor import StepContext


def _ctx(inputs, *, run_id="r", step_id="s", ticket=None):
    return StepContext(
        run_id=run_id,
        step_id=step_id,
        kind="scripted",
        step={},
        inputs=inputs,
        workflow={},
        target_ticket=ticket,
    )


def test_gate_passes_with_no_serious_findings() -> None:
    out = steps.gate(_ctx({"findings": [{"severity": "low"}], "policy": "default"}))
    assert out["verdict"] == "pass"
    assert out["passed"] is True
    assert out["policy_version"] == "1"


def test_gate_fails_on_high_severity() -> None:
    out = steps.gate(_ctx({"findings": [{"severity": "high"}], "policy": "default"}))
    assert out["verdict"] == "fail"
    assert out["failing_count"] == 1


def test_gate_strict_fails_on_any_finding() -> None:
    out = steps.gate(_ctx({"findings": [{"severity": "low"}], "policy": "strict"}))
    assert out["verdict"] == "fail"  # max_findings=0


def test_gate_advisory_never_fails() -> None:
    out = steps.gate(_ctx({"findings": [{"severity": "critical"}], "policy": "advisory"}))
    assert out["verdict"] == "pass"


def test_gate_unknown_policy_falls_back_to_default() -> None:
    out = steps.gate(_ctx({"findings": [{"severity": "critical"}], "policy": "nope"}))
    assert out["policy"] == "nope"
    assert out["verdict"] == "fail"  # default fails on critical


def test_gate_handles_empty_and_malformed() -> None:
    assert steps.gate(_ctx({}))["verdict"] == "pass"
    assert steps.gate(_ctx({"findings": "notalist"}))["verdict"] == "pass"


def test_render_context_builds_sections() -> None:
    out = steps.render_context(_ctx({"diff": "patch text", "meta": {"n": 1}, "empty": ""}))
    ctx_text = out["context"]
    assert "## diff" in ctx_text and "patch text" in ctx_text
    assert "## meta" in ctx_text and '"n": 1' in ctx_text
    assert "## empty" not in ctx_text  # empties skipped
