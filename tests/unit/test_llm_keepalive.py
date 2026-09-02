"""Default-level keepalive emission for long LLM gate operations."""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import LLMConfig
from rebar.llm.runaway_guard import ToolCallLedger
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


def _reset_keepalive_if_present() -> None:
    try:
        from rebar.llm import keepalive
    except ImportError:
        return
    keepalive._reset_for_tests()


def _keepalive_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "rebar.llm.keepalive" and r.levelno >= logging.WARNING
    ]


def test_runner_keepalive_reaches_default_warning_level(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REBAR_LOG_LEVEL", raising=False)
    _reset_keepalive_if_present()
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = _cfg()
    req = RunRequest(
        system_prompt="s", instructions="i", config=cfg, reviewers=["plan-reviewer"], mode="text"
    )
    with caplog.at_level(logging.WARNING):
        PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)

    messages = _keepalive_messages(caplog)
    assert len(messages) == 1
    assert "phase=llm-call-start" in messages[0]
    assert "op=plan-reviewer" in messages[0]
    assert "call=1" in messages[0]
    assert "elapsed=" in messages[0]
    assert "sensitive prompt" not in messages[0]


class _WrappedToolset:
    async def call_tool(self, name, tool_args, ctx, tool):
        return "ok"


async def _run_tool_burst(toolset, *, count: int) -> None:
    ctx = SimpleNamespace(run_step=1)
    for _ in range(count):
        await toolset.call_tool("custom_probe", {"secret": "do-not-log"}, ctx, None)


def test_tool_keepalives_are_coalesced(caplog: pytest.LogCaptureFixture) -> None:
    from rebar.llm.agent_call import _memo_toolsets

    _reset_keepalive_if_present()
    toolset = _memo_toolsets([], [_WrappedToolset()], ledger=ToolCallLedger())[0]
    with caplog.at_level(logging.WARNING):
        asyncio.run(_run_tool_burst(toolset, count=5))

    messages = _keepalive_messages(caplog)
    assert len(messages) == 1
    assert "phase=tool-call-start" in messages[0]
    assert "op=custom_probe" in messages[0]
    assert "secret" not in messages[0]
    assert "do-not-log" not in messages[0]


def test_keepalive_reemits_after_interval(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm import keepalive

    keepalive._reset_for_tests()
    now = {"value": 100.0}
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: now["value"])

    with caplog.at_level(logging.WARNING):
        keepalive.emit_keepalive("llm-call-start", operation="plan-reviewer", started_at=90.0)
        now["value"] += keepalive.KEEPALIVE_INTERVAL_S - 1
        keepalive.emit_keepalive("llm-call-start", operation="plan-reviewer", started_at=90.0)
        now["value"] += 1
        keepalive.emit_keepalive("llm-call-start", operation="plan-reviewer", started_at=90.0)

    messages = _keepalive_messages(caplog)
    assert len(messages) == 2
    assert "call=1" in messages[0]
    assert "call=2" in messages[1]


class _SlowWrappedToolset:
    def __init__(self, clock: dict[str, float]) -> None:
        self._clock = clock

    async def call_tool(self, name, tool_args, ctx, tool):
        self._clock["value"] += 26.0
        return "ok"


def test_tool_completion_keepalive_reports_tool_elapsed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm import agent_call, keepalive
    from rebar.llm.agent_call import _memo_toolsets

    keepalive._reset_for_tests()
    clock = {"value": 100.0}
    monkeypatch.setattr(agent_call.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(keepalive.time, "monotonic", lambda: clock["value"])
    toolset = _memo_toolsets([], [_SlowWrappedToolset(clock)], ledger=ToolCallLedger())[0]

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run_tool_burst(toolset, count=1))

    messages = _keepalive_messages(caplog)
    assert len(messages) == 2
    assert "phase=tool-call-start" in messages[0]
    assert "elapsed=0.0s" in messages[0]
    assert "phase=tool-call-complete" in messages[1]
    assert "elapsed=26.0s" in messages[1]


def test_concurrent_keepalives_share_one_interval(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        from rebar.llm import keepalive
    except ImportError:
        messages: list[str] = []
    else:
        keepalive._reset_for_tests()
        monkeypatch.setattr(keepalive.time, "monotonic", lambda: 100.0)

        def emit() -> None:
            keepalive.emit_keepalive("tool-call-start", operation="parallel_probe", started_at=90.0)

        with caplog.at_level(logging.WARNING):
            threads = [threading.Thread(target=emit) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        messages = _keepalive_messages(caplog)

    assert len(messages) == 1
    assert "op=parallel_probe" in messages[0]
