"""Concurrent read requests on the real MCP server complete independently.

Nine parallel ``next_batch`` calls once took the box's server down together: each held
its own full copy of the store until the container ran out of memory, and every sibling
request failed with it (teenage-unquieted-ringworm). This drives the production server
from :func:`rebar.mcp_server.build_server`, with its sync tools offloaded to worker
threads, and checks two things: a burst of reads all return their own correct result,
and a read that is still running does not hold up or fail the others."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import anyio
import pytest

import rebar

_BURST = 9


def _create(tracker: Path, tid: str, ticket_type: str, parent: str | None, ts: int) -> None:
    d = tracker / tid
    d.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": "CREATE",
        "ticket_id": tid,
        "timestamp": ts,
        "uuid": f"cr-{tid}",
        "env_id": "test",
        "author": "test",
        "data": {
            "ticket_id": tid,
            "title": tid,
            "ticket_type": ticket_type,
            "status": "open",
            "priority": 2,
            "parent_id": parent,
        },
    }
    (d / "001-CREATE.json").write_text(json.dumps(event))


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracker = tmp_path / "tracker"
    ts = 1700004000000000000
    _create(tracker, "cr-epic", "epic", None, ts)
    for i in (1, 2):
        _create(tracker, f"cr-task-{i}", "task", "cr-epic", ts + i)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tracker))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    from rebar.mcp_server import build_server

    return build_server()


def test_a_burst_of_reads_completes_while_a_slow_read_is_still_running(server, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    real_show = rebar.show_ticket

    def slow_show(*args, **kwargs):
        entered.set()
        # A ceiling on how long the siblings get, not a latency budget: they finish in
        # well under it, and the test releases this read as soon as they have.
        release.wait(timeout=60)
        return real_show(*args, **kwargs)

    monkeypatch.setattr(rebar, "show_ticket", slow_show)

    async def scenario() -> tuple[list[str], bool, str]:
        slow: dict[str, str] = {}
        burst: list[str] = []

        async def slow_call() -> None:
            slow["value"] = str(await server.call_tool("show_ticket", {"ticket_id": "cr-epic"}))

        async def batch_call() -> None:
            burst.append(str(await server.call_tool("next_batch", {"epic_id": "cr-epic"})))

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow_call)
            while not entered.is_set():  # noqa: ASYNC110 - set from the worker thread.
                await anyio.sleep(0.005)
            with anyio.fail_after(30):
                async with anyio.create_task_group() as siblings:
                    for _ in range(_BURST):
                        siblings.start_soon(batch_call)
            slow_still_running = "value" not in slow
            release.set()
        return burst, slow_still_running, slow["value"]

    burst, slow_still_running, slow_value = anyio.run(scenario)

    assert slow_still_running, "the slow read finished before the burst; nothing overlapped"
    assert len(burst) == _BURST
    for result in burst:
        assert "cr-task-1" in result and "cr-task-2" in result, result
    assert "cr-epic" in slow_value
