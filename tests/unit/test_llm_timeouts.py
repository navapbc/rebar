"""Activity-based liveness: per-request read timeout + per-tool timeout (story
chief-contained-hoopoe, epic jira-reb-687). Offline, no billable call.

The per-request READ timeout reuses ``cfg.timeout_s`` and is set as an ``httpx.Timeout`` on
arcticduck's shared client (authoritative on the anthropic path). The per-TOOL timeout
(``Agent(tool_timeout=cfg.llm_tool_timeout_s)``) bounds an ASYNC/MCP tool — verified here to
cancel one — while a SYNC in-process tool is NOT interrupted (async cancel can't stop a
blocking call); the sync caveat is pinned so the scope is honest. Step caps (arawana) bound
runaway loops. No total-runtime timeout and no new event loop are introduced.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import DEFAULT_LLM_TOOL_TIMEOUT_S, LLMConfig
from rebar.llm.runner import _build_retrying_anthropic_model

pytestmark = pytest.mark.unit


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


# ── Per-request read timeout: wired onto arcticduck's shared client ───────────
def test_helper_uses_the_supplied_http_timeout():
    """hoopoe passes an httpx.Timeout(read=cfg.timeout_s, ...) into arcticduck's helper;
    the constructed client carries exactly that timeout."""
    t = httpx.Timeout(read=123.0, connect=10.0, write=30.0, pool=10.0)
    _model, http_client = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(), http_timeout=t
    )
    assert http_client.timeout.read == 123.0
    assert http_client.timeout.connect == 10.0


def test_helper_default_timeout_falls_back_to_cfg_timeout_s():
    """Absent an explicit http_timeout, the client is still bounded (never unbounded) —
    the default derives from cfg.timeout_s."""
    _model, http_client = _build_retrying_anthropic_model(
        "claude-sonnet-4-6", base_url=None, cfg=_cfg(timeout_s=321)
    )
    assert http_client.timeout.read == 321.0


# ── Per-tool timeout: cancels an ASYNC tool; a SYNC tool is NOT interrupted ────
def _tool_calling_model():
    state = {"n": 0}

    def gen(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="slow", args={})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(gen)


def test_tool_timeout_cancels_an_async_tool():
    """A hung ASYNC tool is cancelled at ~tool_timeout (bounded liveness); the run
    continues (a soft tool error goes back to the model — no exception raised)."""
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        agent = Agent(_tool_calling_model(), tool_timeout=0.3)

        @agent.tool_plain
        async def slow() -> str:
            await asyncio.sleep(5.0)
            return "never"

        t0 = time.monotonic()
        result = agent.run_sync("go")
        elapsed = time.monotonic() - t0
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    assert elapsed < 2.0  # cancelled well before the 5s sleep
    assert "done" in str(result.output)  # the run recovered, not aborted


def test_sync_tool_is_not_interrupted_documented_caveat():
    """The honest caveat: async cancellation cannot interrupt a SYNC blocking tool, so
    tool_timeout is a no-op for rebar's sync in-process tools (bounded instead by step
    caps). Pinned with a SHORT sync sleep so the scope claim reflects reality."""
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
    try:
        agent = Agent(_tool_calling_model(), tool_timeout=0.1)

        @agent.tool_plain
        def slow() -> str:
            time.sleep(0.6)  # short, but > tool_timeout — a SYNC blocking call
            return "finished"

        t0 = time.monotonic()
        agent.run_sync("go")
        elapsed = time.monotonic() - t0
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    assert elapsed >= 0.6  # NOT cancelled — waited the full sync sleep (the caveat)


# ── Config ────────────────────────────────────────────────────────────────────
def test_tool_timeout_config_default():
    assert LLMConfig(repo_path=".").llm_tool_timeout_s == DEFAULT_LLM_TOOL_TIMEOUT_S == 120


def test_tool_timeout_config_env_override(monkeypatch):
    monkeypatch.setenv("REBAR_LLM_TOOL_TIMEOUT_S", "45")
    assert LLMConfig.from_env(repo_root=".").llm_tool_timeout_s == 45


# ── The runner wires tool_timeout onto the Agent (via a spy) ──────────────────
def test_runner_sets_tool_timeout_on_the_agent(monkeypatch):
    """A model_override run still builds the Agent with tool_timeout in its kwargs — the
    liveness bound is applied on every agentic construction."""
    import rebar.llm.runner as runner_mod
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}
    real_import = runner_mod._import_pydantic_ai

    def _spy_import():
        RealAgent = real_import()

        class _SpyAgent(RealAgent):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                captured["tool_timeout"] = kwargs.get("tool_timeout")
                super().__init__(*args, **kwargs)

        return _SpyAgent

    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = _cfg(llm_tool_timeout_s=77)
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert captured["tool_timeout"] == 77.0


# ── No total-runtime timeout / no total-runtime timer in the gate path ────────
def test_no_total_runtime_timeout_mechanism():
    """Structural guard: the runner introduces NO total-runtime timer (no signal.alarm,
    no wall-clock deadline thread) — liveness is per-request + per-tool + step caps only.
    The single asyncio.run is the client-teardown aclose, not a run-bounding loop."""
    import inspect

    import rebar.llm.runner as runner_mod

    src = inspect.getsource(runner_mod)
    assert "signal.alarm" not in src  # no SIGALRM wall-clock kill
    assert "Timer(" not in src  # no threading.Timer wall-clock deadline
    # The only asyncio.run CALL is the client-teardown aclose (story arcticduck), not a
    # run-bounding loop.
    assert "asyncio.run(_http_client.aclose())" in src
