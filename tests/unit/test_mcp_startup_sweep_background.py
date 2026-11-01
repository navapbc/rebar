"""The MCP boot ensure sweep runs after the listener starts, not before it.

``main()`` used to run the ensure registry synchronously before building and serving the
server. Since the ``snapshot-close-metadata`` unit, a converged sweep costs about 20 s on
a large store. A crash-restarted container therefore refused connections for 25-47 s,
longer than Claude Code's 5-attempt reconnect window, and every connected session stayed
disconnected (bug short-elderly-cock).

These tests block the sweep on an event and prove that: ``main()`` reaches ``run_mcp``
while the sweep is still running; the server answers an MCP ``initialize`` meanwhile; the
sweep holds the drain gauge, so the shutdown idle wait waits for it; and the lock budget
and the fail-closed removed-input path are unchanged.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# A ceiling on how long a blocked sweep waits, not a latency budget: every test releases
# it as soon as it has made its observation.
_CEILING = 30


class _BlockedSweep:
    """A stand-in for ``run_ensures`` that blocks until released and records its call."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tracker: str, **kwargs: Any) -> list[Any]:
        self.calls.append((tracker, kwargs))
        self.entered.set()
        self.release.wait(timeout=_CEILING)
        self.finished.set()
        return []


@pytest.fixture
def blocked_sweep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    import rebar._store.ensures as ensures

    sweep = _BlockedSweep()
    monkeypatch.setattr(ensures, "run_ensures", sweep)
    yield sweep
    sweep.release.set()


def test_main_reaches_run_mcp_while_the_boot_sweep_is_still_running(
    blocked_sweep: _BlockedSweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rebar._logging
    import rebar._mcp_health as health
    from rebar import mcp_server

    monkeypatch.setattr(sys, "argv", ["rebar-mcp"])
    monkeypatch.setattr(rebar._logging, "install_stderr_handler", lambda _name: None)
    monkeypatch.setattr(mcp_server, "compose_startup_opcert_binding", lambda _cfg: None)
    seen: dict[str, Any] = {}

    def _fake_run_mcp(server: Any, _mcp_cfg: Any, *, opcert_binding: Any = None) -> None:
        seen["entered"] = blocked_sweep.entered.wait(timeout=_CEILING)
        seen["finished_before_serving"] = blocked_sweep.finished.is_set()
        seen["gauge"] = getattr(server, health._GAUGE_ATTR).value
        blocked_sweep.release.set()

    monkeypatch.setattr(health, "run_mcp", _fake_run_mcp)

    mcp_server.main()

    assert seen["entered"] is True, "the boot sweep must still run"
    assert seen["finished_before_serving"] is False, (
        "main() must start serving while the boot sweep is still running"
    )
    assert seen["gauge"] == 1, "the running sweep must hold the server's drain gauge"


def test_server_answers_initialize_while_the_boot_sweep_is_blocked(
    blocked_sweep: _BlockedSweep,
) -> None:
    from starlette.testclient import TestClient

    from rebar._mcp_health import _GAUGE_ATTR, start_startup_store_sweep
    from rebar.mcp_server import build_server

    server = build_server()
    thread = start_startup_store_sweep(getattr(server, _GAUGE_ATTR))
    assert thread is not None
    assert blocked_sweep.entered.wait(timeout=_CEILING)

    with TestClient(server.streamable_http_app()) as client:
        response = client.post(
            "/mcp",
            headers={
                "host": "127.0.0.1:8000",
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                }
            ),
        )

    assert response.status_code == 200, response.text
    assert not blocked_sweep.finished.is_set(), "the sweep must still be blocked"
    blocked_sweep.release.set()
    thread.join(timeout=_CEILING)


def test_sweep_holds_the_gauge_and_shutdown_waits_for_it(blocked_sweep: _BlockedSweep) -> None:
    from rebar._mcp_health import InFlightGauge, start_startup_store_sweep, wait_for_idle

    gauge = InFlightGauge()
    thread = start_startup_store_sweep(gauge)
    assert thread is not None
    assert blocked_sweep.entered.wait(timeout=_CEILING)
    assert gauge.value == 1

    idle = threading.Event()
    waiter = threading.Thread(
        target=lambda: (wait_for_idle(gauge, poll_interval=0.01), idle.set()), daemon=True
    )
    waiter.start()
    assert not idle.wait(timeout=0.3), "the idle wait must not return while the sweep runs"

    blocked_sweep.release.set()
    assert idle.wait(timeout=_CEILING), "the idle wait must return once the sweep ends"
    thread.join(timeout=_CEILING)
    assert gauge.value == 0


def test_sweep_keeps_the_boot_lock_budget(blocked_sweep: _BlockedSweep) -> None:
    from rebar._mcp_health import start_startup_store_sweep

    blocked_sweep.release.set()
    thread = start_startup_store_sweep(None)
    assert thread is not None
    thread.join(timeout=_CEILING)
    assert len(blocked_sweep.calls) == 1
    assert blocked_sweep.calls[0][1] == {"timeout": 5, "attempts": 1}


def test_removed_input_still_aborts_startup_synchronously(
    blocked_sweep: _BlockedSweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rebar.config
    from rebar._deprecations import RemovedInputError
    from rebar._mcp_health import InFlightGauge, start_startup_store_sweep

    def _removed() -> str:
        raise RemovedInputError("removed input under test")

    monkeypatch.setattr(rebar.config, "tracker_dir", _removed)
    gauge = InFlightGauge()

    with pytest.raises(RemovedInputError):
        start_startup_store_sweep(gauge)

    assert gauge.value == 0, "no sweep thread may start or hold the gauge"
    assert blocked_sweep.calls == []


def test_missing_store_starts_no_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from rebar._mcp_health import InFlightGauge, start_startup_store_sweep

    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "absent"))
    gauge = InFlightGauge()

    assert start_startup_store_sweep(gauge) is None
    assert gauge.value == 0
