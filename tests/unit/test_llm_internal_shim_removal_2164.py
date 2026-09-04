from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart

from rebar.llm import usage_log

pytestmark = pytest.mark.unit


def _messages() -> list[object]:
    return [
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "a.py"})]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "a.py"})]),
    ]


def test_run_shape_is_the_only_usage_shape_reducer_and_keeps_failure_row_fields(monkeypatch):
    """Happy path: callers use the outcome-neutral reducer, and failed-call JSON is unchanged."""
    captured: list[dict] = []
    monkeypatch.setattr(usage_log, "record", lambda row, **kw: captured.append({**row, **kw}))

    expected = {
        "requests": 2,
        "tool_calls": 2,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "finish_reason": None,
        "request_limit": 3,
        "tool_calls_limit": 8,
        "distinct_fetches": [{"tool": "read_file", "target": "a.py"}],
        "tool_calls_distinct": 1,
        "distinct_ratio_window": None,
        "top_repeated_tool_calls": [{"signature": "read_file:b882bfed", "count": 2}],
        "max_consecutive_repeat": 2,
    }

    assert usage_log.run_shape(_messages(), request_limit=3, tool_calls_limit=8) == expected
    old_name = "failure" + "_usage"
    assert not hasattr(usage_log, old_name)

    usage_log.record_failure(_messages(), "plan-reviewer", "claude-opus-4-8", 3, 4)
    assert captured == [
        {
            **expected,
            "op": "plan-reviewer",
            "step": None,
            "model_class": "claude-opus-4-8",
            "model": "claude-opus-4-8",
            "provider": "anthropic",
            "outcome": usage_log.OUTCOME_FAILED,
        }
    ]


def test_run_shape_record_writes_the_same_durable_json_fields(monkeypatch, tmp_path):
    target = tmp_path / "usage.jsonl"
    monkeypatch.setenv(usage_log.ENV_VAR, str(target))
    row = usage_log.run_shape(_messages(), request_limit=3, tool_calls_limit=8)

    usage_log.record(row, op="plan-reviewer", model="claude-opus-4-8", provider="anthropic")

    written = json.loads(target.read_text().strip())
    assert isinstance(written.pop("timestamp"), str)
    assert written == {
        "op": "plan-reviewer",
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "requests": 2,
        "tool_calls": 2,
        "tool_calls_distinct": 1,
        "max_consecutive_repeat": 2,
        "top_repeated_tool_calls": [{"signature": "read_file:b882bfed", "count": 2}],
        "request_limit": 3,
        "tool_calls_limit": 8,
        "finish_reason": None,
        "distinct_fetches": [{"tool": "read_file", "target": "a.py"}],
        "outcome": usage_log.OUTCOME_OK,
    }
