"""Phase-2 async gate surface (bug d80d): ``*_start`` handles + durable poll.

Phase 1 de-dups a concurrent retry; Phase 2 removes the client timeout from the
request path entirely with an async ``review_plan_start`` / ``verify_completion_start``
that return a durable ``job_id`` in milliseconds while the gate runs on a background
daemon thread. These tests pin the four behaviours the ticket calls out — a sub-100ms
handle while the gate blocks, a poll that transitions running -> passed, a duplicate
``*_start`` that shares ONE run's job_id, and a fresh (post-restart) registry that still
resolves the durable verdict from the on-disk store — deterministically and with zero
tokens (the gate is a fake that blocks on a ``threading.Event``).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import rebar
from rebar import _mcp_inflight as inflight
from rebar._mcp_llm import register_llm_tools
from rebar.llm import gate_runs


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
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-d80d")
    rebar.init_repo(repo_root=str(repo))
    inflight.reset_registry()
    return repo


def _server_with_llm_tools():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("gate-async-test")
    ctx = SimpleNamespace(allow_llm=lambda: True, readonly=lambda: False)
    register_llm_tools(mcp, ctx)
    return mcp


class _BlockingReviewPlan:
    """A stand-in for ``rebar.llm.review_plan`` that blocks until released, counting calls."""

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(
        self, ticket_id, *, ref=None, source=None, sign=True, emit_sidecar=True, force=False
    ):
        with self._lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return {"verdict": "PASS", "ticket_id": ticket_id}


# ── begin_gate_job: the non-blocking singleflight reservation ────────────────────


def test_begin_gate_job_attaches_to_an_inflight_key_with_the_same_job_id():
    inflight.reset_registry()
    first = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    assert first.is_new is True
    second = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    assert second.is_new is False
    assert second.job_id == first.job_id, "a duplicate *_start must share the in-flight job_id"


def test_begin_gate_job_gives_a_distinct_job_for_a_different_basis():
    inflight.reset_registry()
    a = inflight.begin_gate_job("plan_review", "d80d", ref="deadbeef", variant="source=attested")
    b = inflight.begin_gate_job("plan_review", "d80d", ref="feedface", variant="source=attested")
    assert a.job_id != b.job_id


def test_begin_gate_job_force_never_attaches():
    inflight.reset_registry()
    a = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    b = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested", force=True)
    assert b.is_new is True
    assert b.job_id != a.job_id


# ── gate_run_status: resolving a handle to a poll record ─────────────────────────


def test_gate_status_unknown_for_an_unrecorded_job(store):
    assert gate_runs.gate_run_status("no-such-job")["status"] == "unknown"


def test_gate_status_running_then_terminal_from_the_index(store):
    inflight.reset_registry()
    # A recorded run whose daemon is still active reads 'running'…
    handle = inflight.begin_gate_job("plan_review", "d80d", variant="source=attested")
    gate_runs.record_gate_run(
        {
            "job_id": handle.job_id,
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time(),
        }
    )
    assert gate_runs.gate_run_status(handle.job_id)["status"] == "running"
    # …and the recorded terminal status wins once the daemon settles.
    handle.complete(result={"verdict": "PASS"})
    gate_runs.record_gate_run(
        {
            "job_id": handle.job_id,
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    out = gate_runs.gate_run_status(handle.job_id)
    assert out["status"] == "passed"
    assert out["verdict"] == "PASS"


def test_gate_status_stale_running_when_daemon_gone_and_index_still_running(store):
    inflight.reset_registry()
    # An index that reads 'running' with NO active daemon and past the grace window is a
    # crashed leader — surfaced as stale-running (the run_finished marker never fired).
    gate_runs.record_gate_run(
        {
            "job_id": "wedged-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "running",
            "started_at": time.time() - (gate_runs._STALE_GRACE_SECONDS + 1.0),
        }
    )
    assert gate_runs.gate_run_status("wedged-job")["status"] == "stale-running"


def test_gate_status_fresh_registry_resolves_the_durable_verdict(store):
    # Simulate a NEW CONTAINER: the gate ran to completion and recorded its terminal
    # verdict to the on-disk store, then the in-process registry is wiped. A poll must
    # still resolve the durable outcome from disk (never a re-run).
    gate_runs.record_gate_run(
        {
            "job_id": "durable-job",
            "ticket_id": "d80d",
            "gate_type": "plan_review",
            "status": "passed",
            "verdict": "PASS",
            "finished_at": time.time(),
        }
    )
    inflight.reset_registry()  # the fresh registry has no in-memory trace of the run
    out = gate_runs.gate_run_status("durable-job")
    assert out["status"] == "passed"
    assert out["verdict"] == "PASS"


def test_verify_completion_status_unsigned_without_an_attestation(store):
    tid = rebar.create_ticket("bug", "no completion attestation yet")
    out = gate_runs.verify_completion_status(tid)
    assert out["ok"] is False
    assert out["verdict"] == "unsigned"


# ── Tool-level: the async start surface end to end ───────────────────────────────


def test_review_plan_start_returns_a_handle_fast_then_polls_to_passed(store, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    holder: dict = {}

    async def scenario():
        t0 = time.monotonic()
        started = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        holder["elapsed"] = time.monotonic() - t0
        holder["started"] = started

    anyio.run(scenario)

    started = holder["started"]
    assert started["status"] == "running"
    assert holder["elapsed"] < 0.5, "async start must return a handle without blocking on the gate"
    job_id = started["job_id"]
    # Wait for the daemon to enter the gate, then release it and poll to a terminal verdict.
    assert fake.started.wait(timeout=5)
    assert gate_runs.gate_run_status(job_id)["status"] == "running"
    fake.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate_runs.gate_run_status(job_id)["status"] != "running":
            break
        time.sleep(0.02)
    out = gate_runs.gate_run_status(job_id)
    assert out["status"] == "passed"
    assert fake.calls == 1


def test_duplicate_review_plan_start_shares_one_job_and_one_run(store, monkeypatch):
    inflight.reset_registry()
    fake = _BlockingReviewPlan()
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_plan", fake)
    mcp = _server_with_llm_tools()

    holder: dict = {}

    async def scenario():
        a = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        # Second start WHILE the first run is still blocked inside the gate.
        assert fake.started.wait(timeout=5)
        b = await mcp._tool_manager.call_tool("review_plan_start", {"ticket_id": "d80d"})
        holder["a"], holder["b"] = a, b

    anyio.run(scenario)
    assert holder["a"]["job_id"] == holder["b"]["job_id"], "a duplicate start must share the job_id"
    fake.release.set()
    # Give the single daemon a moment to settle; the gate ran exactly once.
    time.sleep(0.2)
    assert fake.calls == 1
