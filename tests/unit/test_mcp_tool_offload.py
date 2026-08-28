"""A synchronous MCP tool body must not occupy the event loop.

WHY THIS TEST EXISTS. Every rebar MCP tool is a plain ``def``, and the SDK calls a sync tool
body DIRECTLY inside the ASGI request coroutine (``mcp/server/fastmcp/utilities/
func_metadata.py``: ``if fn_is_async: await fn(...) else: fn(...)``). On the stdio transport
that was harmless -- one client, one process. Behind the SHARED HTTP transport it is not: the
call occupies the uvicorn event loop for its whole duration and the server answers nothing
else meanwhile, including the unauthenticated ``/health`` route.

MEASURED on the deployed box (bug dewy-rotatable-tarsier): a ``CallToolRequest`` began at
21:03:00.590 and the process logged NOTHING for 3m56s; at 21:06:56.275 six ``/health``
responses and five 401s completed within 5 MILLISECONDS of each other -- a backlog draining
the instant the loop was released. From outside that window is `http=000` after 70s and 401s
taking 12s/28s/50s/63s against a 0.25s steady state, which is why the bug was reported as a
slow first `initialize` after a redeploy: a redeploy makes every agent reconnect at once, so
the first one to arrive is the one most likely to queue behind another client's long tool
call. The handshake itself measures 30 ms cold.

WHAT THE FIRST TEST PINS, and why it is shaped this way. The assertion is NOT a wall-clock
threshold -- those are flaky and say nothing about the mechanism. Instead the tool body waits
on a ``threading.Event`` that ONLY a concurrently-scheduled coroutine can set. If the loop is
free the coroutine runs and the body observes ``released``; if the loop is blocked the
coroutine cannot be scheduled at all and the body observes its own timeout. The verdict is a
value, not a duration, so the test is deterministic in what it proves.
"""

from __future__ import annotations

import contextvars
import threading

import anyio
import pytest

from rebar._mcp_health import InFlightGauge, offload_sync_tools, wire_health


def _server_with_slow_sync_tool():
    """A FastMCP server whose one sync tool reports whether the loop stayed free."""

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    # MCP v1 leaves Settings.lifespan unresolved until the model is rebuilt after FastMCP is
    # defined; mcp_server.build_server does the same before constructing a server.
    Settings.model_rebuild()
    mcp = FastMCP("offload-test")
    entered = threading.Event()
    release = threading.Event()
    observed: dict[str, str] = {}

    @mcp.tool()
    def slow_sync() -> str:
        observed["thread"] = threading.current_thread().name
        entered.set()
        # 2s is a generous ceiling on "a coroutine gets a turn", not a latency budget: when
        # the loop is free the wait is released in microseconds, and when it is blocked no
        # amount of waiting would help.
        return "released" if release.wait(timeout=2.0) else "blocked"

    return mcp, entered, release, observed


async def _call_while_something_else_runs(mcp, entered, release) -> str:
    """Call ``slow_sync`` while a second coroutine races to release it."""

    result: dict[str, object] = {}

    async def run_tool() -> None:
        result["value"] = await mcp._tool_manager.call_tool("slow_sync", {})

    async def concurrent_request() -> None:
        # Stands in for every other request the server owes: /health, initialize, tools/list.
        # ASYNC110 (poll instead of anyio.Event) is deliberate: the event is set from a
        # WORKER thread, and anyio.Event.set() is not safe to call off the event loop. The
        # poll is also the point -- it is what a blocked loop prevents from ever running.
        while not entered.is_set():  # noqa: ASYNC110
            await anyio.sleep(0.005)
        release.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_tool)
        task_group.start_soon(concurrent_request)
    return str(result["value"])


def test_a_slow_sync_tool_does_not_block_concurrent_work() -> None:
    """THE regression test. Without the offload this returns "blocked"."""

    mcp, entered, release, _ = _server_with_slow_sync_tool()
    offload_sync_tools(mcp)

    verdict = anyio.run(_call_while_something_else_runs, mcp, entered, release)

    assert "released" in verdict, (
        "the event loop was occupied by the sync tool body, so no other request could be "
        "served while it ran -- this is bug dewy-rotatable-tarsier"
    )


def test_a_sync_tool_body_runs_off_the_event_loop_thread() -> None:
    mcp, entered, release, observed = _server_with_slow_sync_tool()
    offload_sync_tools(mcp)

    loop_thread = threading.current_thread().name
    anyio.run(_call_while_something_else_runs, mcp, entered, release)

    assert observed["thread"] != loop_thread


def test_contextvars_bound_by_the_caller_reach_the_offloaded_body() -> None:
    """Load-bearing, not incidental: ``run_http_with_grace`` binds the box's op-cert signer as
    a ContextVar inside the serving thread and the certified tools mint op-certs from it. A
    worker thread that did not inherit that context would sign under the wrong environment."""

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unbound")
    mcp = FastMCP("offload-ctx")

    @mcp.tool()
    def read_marker() -> str:
        return marker.get()

    offload_sync_tools(mcp)

    async def scenario() -> str:
        marker.set("bound-by-caller")
        return str(await mcp._tool_manager.call_tool("read_marker", {}))

    assert "bound-by-caller" in anyio.run(scenario)


def test_an_already_async_tool_is_left_alone() -> None:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("offload-async")

    @mcp.tool()
    async def already_async() -> str:
        return "ok"

    original = mcp._tool_manager.get_tool("already_async").fn
    moved = offload_sync_tools(mcp)

    assert moved == 0
    assert mcp._tool_manager.get_tool("already_async").fn is original


def test_the_offload_reports_how_many_tools_it_moved() -> None:
    mcp, _entered, _release, _observed = _server_with_slow_sync_tool()

    assert offload_sync_tools(mcp) == 1
    # Idempotent: a second pass finds nothing left to move and must not double-wrap.
    assert offload_sync_tools(mcp) == 0


def test_a_server_without_a_tool_manager_is_a_no_op() -> None:
    class _Bare:
        pass

    assert offload_sync_tools(_Bare()) == 0


def test_certified_tools_keep_their_gauge_instrumentation_after_the_offload() -> None:
    """Ordering guard. ``instrument_certified_tools`` installs a SYNC wrapper and skips tools
    already marked async, so an offload that ran FIRST would silently leave the certified
    tools uninstrumented and the SIGTERM drain blind to in-flight billable work. ``wire_health``
    must therefore instrument before it offloads, and this pins that order."""

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("offload-gauge")
    started = threading.Event()
    finish = threading.Event()
    peak = {"value": 0}

    @mcp.tool(name="review_plan")
    def review_plan() -> str:
        started.set()
        finish.wait(timeout=2.0)
        return "done"

    gauge: InFlightGauge = wire_health(mcp)

    async def scenario() -> None:
        async def run_tool() -> None:
            await mcp._tool_manager.call_tool("review_plan", {})

        async def watch() -> None:
            while not started.is_set():  # noqa: ASYNC110 - set from a worker thread; see above
                await anyio.sleep(0.005)
            peak["value"] = gauge.value
            finish.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_tool)
            task_group.start_soon(watch)

    anyio.run(scenario)

    # Non-zero WHILE the tool ran, which is only observable because the offload freed the
    # loop -- the two behaviours are pinned by one assertion on purpose.
    assert peak["value"] == 1
    assert gauge.value == 0


@pytest.mark.parametrize("tool_name", ["slow_sync"])
def test_the_offloaded_tool_still_returns_its_value(tool_name: str) -> None:
    mcp, entered, release, _ = _server_with_slow_sync_tool()
    offload_sync_tools(mcp)
    release.set()
    entered.set()

    async def scenario() -> str:
        return str(await mcp._tool_manager.call_tool(tool_name, {}))

    assert "released" in anyio.run(scenario)
