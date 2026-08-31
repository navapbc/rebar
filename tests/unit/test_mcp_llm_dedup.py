"""Tool-level proof that the MCP gate tools de-duplicate concurrent in-flight calls (bug d80d).

``test_mcp_inflight`` pins the singleflight registry in isolation; this pins the WIRING —
that ``review_plan`` / ``verify_completion``, as registered on the FastMCP server and run on
the server's worker threads (``offload_sync_tools``), actually route through the registry so
two concurrent same-ticket calls invoke the underlying billable ``rebar.llm`` gate EXACTLY
ONCE (AC #2). The gate is monkeypatched with a fake that blocks on a ``threading.Event`` and
counts calls, so the verdict is a call count — deterministic, zero tokens.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import anyio
import pytest

from rebar import _mcp_inflight as inflight
from rebar._mcp_health import offload_sync_tools
from rebar._mcp_llm import register_llm_tools


def _server_with_llm_tools(readonly: bool = False):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("dedup-test")
    ctx = SimpleNamespace(allow_llm=lambda: True, readonly=lambda: readonly)
    register_llm_tools(mcp, ctx)
    # The real server offloads sync tool bodies onto worker threads; do the same so two
    # concurrent CallTool requests genuinely overlap (a sync body on the loop cannot).
    offload_sync_tools(mcp)
    return mcp


class _BlockingReviewPlan:
    def __init__(self):
        self.calls = 0
        self._started = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, ticket_id, **kwargs):
        with self._lock:
            self.calls += 1
        self._started.set()
        self._release.wait(timeout=5)
        return {"verdict": "PASS", "ticket_id": ticket_id}


@pytest.mark.parametrize("tool_name", ["review_plan", "verify_completion"])
def test_two_concurrent_calls_invoke_the_gate_exactly_once(tool_name, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, tool_name, fake)
    # readonly=True keeps both gate bodies store-independent (review_plan skips signing;
    # verify_completion skips the COMPLETION_VERDICT sidecar write) so the test needs no
    # real store — it proves ONLY the singleflight wiring, deterministically + token-free.
    mcp = _server_with_llm_tools(readonly=True)

    # Release the leader only once a *real* follower has attached to its in-flight entry.
    # Spying on ``_await_follower`` (the follower-only branch of ``run_singleflight``) makes
    # the proof deterministic rather than reliant on a fixed sleep: the first CallTool pays a
    # cold pydantic/thread-pool warm-up that blocks the loop, so a fixed window is racy. If
    # de-dup were broken the follower would become a SECOND leader, ``_await_follower`` would
    # never run, and the bounded fallback below still releases so the test fails (calls == 2)
    # instead of hanging — the teeth stay intact.
    follower_attached = threading.Event()
    orig_await_follower = inflight._await_follower

    def _spy_await_follower(entry):
        follower_attached.set()
        return orig_await_follower(entry)

    monkeypatch.setattr(inflight, "_await_follower", _spy_await_follower)

    results: dict[str, object] = {}

    async def scenario():
        async def leader():
            results["a"] = await mcp._tool_manager.call_tool(tool_name, {"ticket_id": "d80d"})

        async def follower():
            # Issue the duplicate WHILE the leader is still blocked inside the gate.
            while not fake._started.is_set():  # noqa: ASYNC110 — set from a worker thread
                await anyio.sleep(0.005)
            results["b"] = await mcp._tool_manager.call_tool(tool_name, {"ticket_id": "d80d"})

        async def releaser():
            deadline = anyio.current_time() + 5.0
            while not follower_attached.is_set() and anyio.current_time() < deadline:  # noqa: ASYNC110
                await anyio.sleep(0.005)
            fake._release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(leader)
            tg.start_soon(follower)
            tg.start_soon(releaser)

    anyio.run(scenario)

    assert fake.calls == 1, "two concurrent same-ticket calls must invoke the gate once"
    # Both callers received a PASS verdict (FastMCP returns a (content, structured) tuple).
    for key in ("a", "b"):
        assert results[key] is not None
