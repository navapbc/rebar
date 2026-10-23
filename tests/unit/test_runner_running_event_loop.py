"""Synchronous agent execution must work when the caller already has a running event
loop. MCP invokes synchronous tool bodies on the ASGI loop thread, where nested
run_until_complete fails. The runner offloads that work and preserves the gate
session context. These tests exercise the running-loop path directly.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")


def _recording_model(json_out: str, seen: dict):
    """An offline pydantic-ai model that records the loop state of the thread it is
    called on, then returns a fixed payload. No network, no tokens."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        try:
            asyncio.get_running_loop()
            seen["ran_under_running_loop"] = True
        except RuntimeError:
            seen["ran_under_running_loop"] = False
        seen["thread"] = threading.current_thread().name
        return ModelResponse(parts=[TextPart(json_out)])

    return FunctionModel(gen)


def _runner_and_request(seen: dict):
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=".")
    runner = PydanticAIRunner(
        cfg, model_override=_recording_model('{"findings": [], "summary": "ok"}', seen)
    )
    req = RunRequest(
        system_prompt="sys",
        instructions="do it",
        config=cfg,
        reviewers=["r"],
        target={"kind": "workflow_step", "ticket_ids": []},
        mode="findings",
    )
    return runner, req


def test_runner_run_succeeds_inside_a_running_event_loop() -> None:
    """THE regression test: ``PydanticAIRunner.run`` called from a coroutine — i.e. exactly
    how a sync MCP tool body executes — must produce its verdict, not die on
    ``This event loop is already running``."""
    seen: dict = {}

    async def call_from_inside_a_running_loop():
        # Precondition proof: this really is the MCP condition, not a bare thread.
        assert asyncio.get_running_loop().is_running()
        runner, req = _runner_and_request(seen)
        return runner.run(req)

    out = asyncio.run(call_from_inside_a_running_loop())

    assert out["findings"] == []
    assert out["summary"] == "ok"
    # The model was actually reached — a "fix" that swallows the failure and returns an
    # empty verdict without calling the model must not pass.
    assert out["_usage"]["requests"] == 1


def test_the_model_call_never_happens_under_a_running_loop() -> None:
    """The CONTRACT, stated as a property rather than an implementation detail: whatever
    thread the synchronous drive ends up on, that thread must not have a running event
    loop — because ``run_until_complete`` is what the drive is about to call there."""
    seen: dict = {}

    async def call_from_inside_a_running_loop():
        runner, req = _runner_and_request(seen)
        return runner.run(req)

    asyncio.run(call_from_inside_a_running_loop())

    assert seen["ran_under_running_loop"] is False


def test_no_loop_path_is_unchanged() -> None:
    """The CLI path — a bare thread with no loop — must not regress.

    Deliberately NOT asserting which thread the model call lands on: pydantic-ai runs a
    ``FunctionModel``'s SYNC generator through its own ``anyio.to_thread`` offload, so thread
    identity here measures a third-party implementation detail, not rebar's contract. The
    contract is the loop state, which is what is asserted."""
    seen: dict = {}
    runner, req = _runner_and_request(seen)

    out = runner.run(req)

    assert out["summary"] == "ok"
    assert out["_usage"]["requests"] == 1
    assert seen["ran_under_running_loop"] is False


def test_running_loop_and_no_loop_produce_the_same_verdict() -> None:
    """Equivalence: the MCP surface must return what the CLI surface returns. A degraded
    or differently-shaped result under a running loop is still a defect."""
    cli_seen: dict = {}
    cli_runner, cli_req = _runner_and_request(cli_seen)
    cli_out = cli_runner.run(cli_req)

    mcp_seen: dict = {}

    async def call_from_inside_a_running_loop():
        runner, req = _runner_and_request(mcp_seen)
        return runner.run(req)

    mcp_out = asyncio.run(call_from_inside_a_running_loop())

    comparable = ("findings", "summary", "runner", "model", "reviewers", "target")
    assert {k: cli_out[k] for k in comparable} == {k: mcp_out[k] for k in comparable}


def test_a_sync_mcp_tool_body_really_does_run_on_the_loop_thread() -> None:
    """Pin the SDK premise that a synchronous MCP tool body executes on the request's
    event-loop thread. An SDK change that offloads it should make this test expose the
    changed environment.
    """
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    server = FastMCP("f643-probe")

    @server.tool()
    def probe() -> str:
        try:
            return "running" if asyncio.get_running_loop().is_running() else "idle"
        except RuntimeError:
            return "no-loop"

    async def drive():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return (await client.call_tool("probe", {})).content[0].text

    assert asyncio.run(drive()) == "running"


def test_the_gate_session_survives_the_hop_off_the_event_loop() -> None:
    """The runner's thread offload must copy the gate-session ContextVar. A bare worker
    thread loses that context, so assert_gated would refuse agentic filesystem tools
    even though the caller entered a gate session.
    """
    from rebar.llm.gate_context import gate_session, in_gate_session, use_code_root
    from rebar.llm.model_classes import drive_off_event_loop

    seen: dict = {}

    def probe():
        from rebar.llm.gate_context import current_code_root

        seen["in_gate_session"] = in_gate_session()
        seen["code_root"] = current_code_root()
        seen["loop_running"] = False
        try:
            asyncio.get_running_loop()
            seen["loop_running"] = True
        except RuntimeError:
            pass
        return "done"

    async def call_from_inside_a_running_loop():
        with gate_session(), use_code_root("/pinned/snapshot"):
            assert in_gate_session() is True  # fixture precondition, on the loop thread
            return drive_off_event_loop(probe)

    assert asyncio.run(call_from_inside_a_running_loop()) == "done"
    assert seen["in_gate_session"] is True, "gate session lost across the thread hop"
    assert seen["code_root"] == "/pinned/snapshot", "pinned read-root lost across the hop"
    assert seen["loop_running"] is False, "the drive landed on a thread with a running loop"
