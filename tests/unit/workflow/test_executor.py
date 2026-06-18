"""Unit tests for the thin linear executor (WS-C2): ordering, scripted/agent
dispatch, named-output wiring, expression resolution, guards, and failure stop."""

from __future__ import annotations

import pytest

from rebar.llm.errors import WorkflowValidationError
from rebar.llm.workflow import executor as ex

jsonschema = pytest.importorskip("jsonschema")


def _echo(ctx):
    # A trivial scripted step: echo its `value` input back as `echoed`.
    return {"echoed": ctx.inputs.get("value")}


def _wf(steps, inputs=None):
    doc = {"schema_version": "1", "name": "t", "steps": steps}
    if inputs:
        doc["inputs"] = inputs
    return doc


def test_static_order_respects_needs() -> None:
    doc = _wf(
        [
            {"id": "c", "uses": "e", "needs": ["b"]},
            {"id": "a", "uses": "e"},
            {"id": "b", "uses": "e", "needs": ["a"]},
        ]
    )
    assert ex.static_order(doc) == ["a", "b", "c"]


def test_scripted_dispatch_and_output_wiring() -> None:
    doc = _wf(
        [
            {"id": "a", "uses": "echo", "with": {"value": "${{ inputs.x }}"}},
            {
                "id": "b",
                "uses": "echo",
                "needs": ["a"],
                "with": {"value": "${{ steps.a.outputs.echoed }}"},
            },
        ],
        inputs={"x": {"type": "string"}},
    )
    res = ex.run_workflow(doc, {"x": "hello"}, run_id="r1", scripted_registry={"echo": _echo})
    assert res.status == "succeeded"
    assert res.outputs["a"]["echoed"] == "hello"
    assert res.outputs["b"]["echoed"] == "hello"  # wired forward
    assert res.terminal_step == "b"
    assert res.terminal_output["echoed"] == "hello"


def test_agent_dispatch_with_fake_runner() -> None:
    doc = _wf([{"id": "rev", "prompt": "code_quality", "mode": "findings"}])
    res = ex.run_workflow(doc, run_id="r")
    assert res.status == "succeeded"
    assert res.outputs["rev"]["_fake"] is True
    assert res.outputs["rev"]["findings"] == []


def test_secret_and_env_resolution(monkeypatch) -> None:
    monkeypatch.setenv("MY_ENV", "envval")
    doc = _wf(
        [{"id": "a", "uses": "echo", "with": {"value": "${{ secrets.S }}", "e": "${env:MY_ENV}"}}]
    )

    def echo2(ctx):
        return {"echoed": ctx.inputs["value"], "e": ctx.inputs["e"]}

    res = ex.run_workflow(doc, run_id="r", scripted_registry={"echo": echo2}, secrets={"S": "shh"})
    assert res.outputs["a"]["echoed"] == "shh"
    assert res.outputs["a"]["e"] == "envval"


def test_whole_expression_preserves_raw_type() -> None:
    # A string that is exactly one expression yields the raw referenced value
    # (a list here), not a stringified one — so findings arrays wire intact.
    doc = _wf(
        [
            {"id": "a", "uses": "mk"},
            {
                "id": "b",
                "uses": "echo",
                "needs": ["a"],
                "with": {"value": "${{ steps.a.outputs.items }}"},
            },
        ]
    )

    def mk(ctx):
        return {"items": [1, 2, 3]}

    res = ex.run_workflow(doc, run_id="r", scripted_registry={"mk": mk, "echo": _echo})
    assert res.outputs["b"]["echoed"] == [1, 2, 3]


def test_failed_step_stops_run() -> None:
    doc = _wf(
        [
            {"id": "a", "uses": "boom"},
            {"id": "b", "uses": "echo", "needs": ["a"]},
        ]
    )

    def boom(ctx):
        raise RuntimeError("kaboom")

    res = ex.run_workflow(doc, run_id="r", scripted_registry={"boom": boom, "echo": _echo})
    assert res.status == "failed"
    assert "kaboom" in res.error
    assert "b" not in res.outputs  # downstream never ran


def test_unknown_scripted_step_fails_gracefully() -> None:
    doc = _wf([{"id": "a", "uses": "nope"}])
    res = ex.run_workflow(doc, run_id="r", scripted_registry={})
    assert res.status == "failed"
    assert "unknown scripted step" in res.error


def test_if_guard_skips_step() -> None:
    doc = _wf(
        [
            {"id": "a", "uses": "echo", "with": {"value": "x"}},
            {
                "id": "b",
                "uses": "echo",
                "needs": ["a"],
                "if": "${{ inputs.run_b }}",
                "with": {"value": "y"},
            },
        ],
        inputs={"run_b": {"type": "boolean"}},
    )
    res = ex.run_workflow(doc, {"run_b": False}, run_id="r", scripted_registry={"echo": _echo})
    assert res.steps["b"] == "skipped"
    assert res.outputs["b"] == {}


def test_invalid_workflow_raises() -> None:
    # Two terminal steps -> a lint error -> WorkflowValidationError before any run.
    doc = _wf([{"id": "a", "uses": "e"}, {"id": "b", "uses": "e"}])
    with pytest.raises(WorkflowValidationError):
        ex.run_workflow(doc, run_id="r", scripted_registry={"e": _echo})


def test_memory_recorder_captures_records() -> None:
    doc = _wf([{"id": "a", "uses": "echo", "with": {"value": "v"}}])
    rec = ex.MemoryRecorder()
    ex.run_workflow(doc, run_id="r", scripted_registry={"echo": _echo}, recorder=rec)
    assert any(s["step_id"] == "a" and s["status"] == "succeeded" for s in rec.steps)
    assert rec.runs[0]["status"] == "running"
    assert rec.runs[-1]["status"] == "succeeded"


def test_runstate_is_immutable_copy_on_write() -> None:
    s0 = ex.RunState(inputs={"x": 1})
    s1 = s0.with_step("a", ex.StepResult(outputs={"o": 1}))
    assert s0.outputs == {}  # original untouched
    assert s1.outputs == {"a": {"o": 1}}
    assert s1.statuses == {"a": "succeeded"}


def test_register_step_decorator() -> None:
    @ex.register_step("registered_demo")
    def _demo(ctx):
        return {"ok": True}

    try:
        assert ex.STEP_REGISTRY["registered_demo"] is _demo
        doc = _wf([{"id": "a", "uses": "registered_demo"}])
        res = ex.run_workflow(doc, run_id="r")  # uses the global registry
        assert res.outputs["a"]["ok"] is True
    finally:
        ex.STEP_REGISTRY.pop("registered_demo", None)
