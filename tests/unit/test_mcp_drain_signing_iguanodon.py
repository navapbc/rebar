"""MCP retirement must not cut off signing work or delete the key under it.

Bug external-taxonomic-iguanodon: autodeploy sent SIGTERM to an MCP container while a
``transition_ticket`` close was in flight on it. The drain only waited for the 4 LLM tools,
so it stopped serving at once; ``run_mcp`` returned, ``mcp_server.main()`` ran
``binding.cleanup()`` and deleted the box key copy, and the still-running close committed
STATUS and then failed to sign. The ticket was left closed with no completion certificate,
which is the audit signal of a forced close.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import anyio.to_thread
import pytest
from _subprocess_env import subprocess_env
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings

import rebar
from rebar._commands import transition_close
from rebar._mcp_health import (
    _GAUGE_ATTR,
    InFlightGauge,
    MCPRetiringError,
    instrument_certified_tools,
    run_mcp,
    wire_health,
)
from rebar._opcert_binding import spawn_context_daemon
from rebar.llm.plan_review.attest import current_material_fingerprint
from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.keyprov import compose_signer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["transition_ticket", "sign_manifest", "sign_review"])
def test_signing_tools_are_drained_and_refused_while_retiring(name):
    started, release = threading.Event(), threading.Event()

    def _body():
        started.set()
        release.wait(timeout=5)
        return "done"

    tool = SimpleNamespace(fn=_body, is_async=False)
    mcp = SimpleNamespace(_tool_manager=SimpleNamespace(get_tool={name: tool}.get))
    gauge = InFlightGauge()
    instrument_certified_tools(mcp, gauge)

    worker = threading.Thread(target=tool.fn)
    worker.start()
    try:
        assert started.wait(timeout=5)
        assert gauge.value == 1, f"{name} in flight must hold the SIGTERM drain open"
        gauge.begin_draining()
        with pytest.raises(MCPRetiringError):
            tool.fn()
    finally:
        release.set()
        worker.join(timeout=5)
    assert gauge.value == 0


def test_spawned_daemon_holds_the_serving_gauge_until_it_ends():
    from rebar._mcp_health import serving_gauge

    gauge = InFlightGauge()
    release = threading.Event()
    with serving_gauge(gauge):
        thread = spawn_context_daemon(lambda: release.wait(timeout=5), name="hold-probe")
    # Counted before spawn returns, so the spawning call can never drop the gauge to 0
    # while its daemon is still unaccounted for.
    assert gauge.value == 1
    gauge.begin_draining()  # accepted background work is waited for, not refused
    release.set()
    thread.join(timeout=5)
    assert gauge.value == 0


def test_spawn_outside_a_serving_context_is_untracked():
    thread = spawn_context_daemon(lambda: None, name="unserved-probe")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_hold_release_is_idempotent():
    gauge = InFlightGauge()
    release = gauge.hold()  # new in this fix
    release()
    release()
    assert gauge.value == 0


# ── end to end: a close that reaches signing after the server stopped ────────────────


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True, env=subprocess_env())
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def box_binding(tmp_path: Path):
    key = tmp_path / "boxkey"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
        env=subprocess_env(),
    )
    binding = compose_signer(OpcertServiceConfig(env_id="box-env", key_path=str(key)))
    yield binding
    binding.cleanup()


def _stop_transport_mid_close(store, binding, monkeypatch, *, worker_starts_late):
    """Close a ticket through the real MCP wiring and stop the transport mid-call.

    ``worker_starts_late`` stops the transport after a worker picked the call up but before
    it ran a line of it; otherwise the transport stops once the close body is running.
    Returns ``(ticket_id, results)``.
    """
    tid = rebar.create_ticket("task", "retiring close", description="## Plan\nD1\n")
    rebar.claim(tid, assignee="t@e.com")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=store,
        check=True,
        capture_output=True,
        text=True,
        env=subprocess_env(),
    ).stdout.strip()
    passed = {
        "verdict": "PASS",
        "model": "m",
        "runner": "r",
        "source": "attested",
        "certifiable": True,
        "verified_at_sha": head,
        "material_fingerprint": current_material_fingerprint(tid),
    }
    monkeypatch.setattr(
        transition_close, "_completion_precheck", lambda *a, **k: (dict(passed), "required")
    )

    # The close commits STATUS, then pauses before signing until the key copy is gone
    # (what happened in the incident), or for 1 s if the server correctly waits for it.
    cleaned_up = threading.Event()
    real_sign = transition_close._sign_completion_and_report

    def _sign_after_retirement(*args, **kwargs):
        cleaned_up.wait(timeout=1.0)
        return real_sign(*args, **kwargs)

    monkeypatch.setattr(transition_close, "_sign_completion_and_report", _sign_after_retirement)
    monkeypatch.setattr("rebar._mcp_serving.run_startup_opcert_check", lambda _b: None)

    # The real wiring: certified instrumentation, then the AnyIO offload (wire_health).
    Settings.model_rebuild()
    mcp = FastMCP("drain-handoff")
    results: list[dict] = []
    in_flight, worker_done = threading.Event(), threading.Event()

    @mcp.tool(name="transition_ticket")
    def _close() -> str:
        in_flight.set()
        results.append(rebar.transition(tid, "in_progress", "closed"))
        return "closed"

    gauge = wire_health(mcp, InFlightGauge())
    real_run_sync = anyio.to_thread.run_sync

    def _worker(func, *args, **kwargs):
        def _run():
            try:
                if worker_starts_late:
                    # Picked up, not one line run yet: free-threaded CPython lets the
                    # serving thread race ahead here (bug leprous-uncivic-crab).
                    in_flight.set()
                    cleaned_up.wait(timeout=1.0)
                return func()
            finally:
                worker_done.set()

        return real_run_sync(_run, *args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _worker)

    class _Server:
        def run(self, transport):
            # FastMCP accepts the call and hands it to a worker; the transport then goes
            # away (stdin closed / uvicorn exited) and abandons the request mid-call.
            async def _serve():
                async with anyio.create_task_group() as tg:
                    tg.start_soon(mcp._tool_manager.call_tool, "transition_ticket", {})
                    while not in_flight.is_set():  # noqa: ASYNC110 - set off-loop
                        await anyio.sleep(0.005)
                    tg.cancel_scope.cancel()

            anyio.run(_serve)

    server = _Server()
    setattr(server, _GAUGE_ATTR, gauge)
    try:
        run_mcp(server, SimpleNamespace(transport="stdio"), opcert_binding=binding)
    finally:
        binding.cleanup()  # mcp_server.main()'s finally
        cleaned_up.set()
        worker_done.wait(timeout=30)
    assert gauge.value == 0
    return tid, results


def test_close_reaching_signing_after_the_server_stops_ends_certified(
    store, box_binding, monkeypatch
):
    tid, results = _stop_transport_mid_close(
        store, box_binding, monkeypatch, worker_starts_late=False
    )

    assert rebar.show_ticket(tid)["status"] == "closed"
    assert results, "the close did not complete"
    assert results[0]["completion_signature"]["signed"] is True, (
        "a close that was in flight when the server stopped must not end closed and "
        f"unsigned: {results[0]['completion_signature']}"
    )
    signatures = list((store / ".tickets-tracker" / tid).glob("*-SIGNATURE.json"))
    assert len(signatures) == 1


def test_close_picked_up_but_not_started_when_the_server_stops_never_runs(
    store, box_binding, monkeypatch
):
    """Bug leprous-uncivic-crab: the close was not counted until its worker ran its first
    line, so the server stopped, the key copy was deleted, and the close then committed
    STATUS and failed to sign. The call is counted from handoff now, and one whose
    caller left before it started is dropped, as AnyIO already drops a cancelled one."""
    tid, results = _stop_transport_mid_close(
        store, box_binding, monkeypatch, worker_starts_late=True
    )

    assert results == [], f"an abandoned, unstarted close must not run: {results}"
    assert rebar.show_ticket(tid)["status"] == "in_progress"
