"""The plan-review engine is selectable on every surface (ADR 0123): the CLI ``--engine`` flag,
the MCP ``review_plan`` tool and the async ``review_plan_start`` + poll path. The MCP dedup key
carries the resolved engine, and gate congestion behaves identically under both engines.
Offline: a FakeRunner (the agent finds nothing, so an agentic review PASSes) in a temp repo.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import rebar
import rebar.llm
from rebar import _mcp_inflight as inflight
from rebar import _mcp_llm
from rebar.config import ConfigError
from rebar.llm import gate_admission as ga
from rebar.llm import gate_runs
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n## Why\nbecause.\n\n"
    "## Acceptance Criteria\n- [ ] the thing is changed\n- [ ] `pytest tests/unit` proves it\n"
)


class _Fake(FakeRunner):
    """Empty, schema-valid answers for every plan-review contract; counts calls."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        self.calls += 1
        if req.mode == "text":
            return {"text": "", "runner": self.name, "model": None, "trace_id": None}
        if req.output_schema == "plan_review_agentic_findings":
            payload: dict = {"findings": []}
        elif req.output_schema == "plan_review_verification":
            payload = {"verifications": []}
        elif req.output_schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), req.output_schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


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
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.delenv("REBAR_PLAN_REVIEW_BUDGET", raising=False)
    rebar.init_repo(repo_root=str(repo))
    inflight.reset_registry()
    return repo


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    runner = _Fake()
    monkeypatch.setattr("rebar.llm.runner.get_runner", lambda *a, **k: runner)
    return runner


def _ticket(store: Path) -> str:
    nonce = f"\nMaterial nonce: {uuid.uuid4().hex}.\n"
    return rebar.create_ticket(
        "task", "surface plan", description=_DESC + nonce, repo_root=str(store)
    )


def _server():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings

    Settings.model_rebuild()
    mcp = FastMCP("engine-surfaces-test")
    _mcp_llm.register_llm_tools(
        mcp, SimpleNamespace(allow_llm=lambda: True, readonly=lambda: False)
    )
    return mcp


def _call(mcp, tool: str, args: dict):
    holder: dict = {}

    async def go():
        holder["out"] = await mcp._tool_manager.call_tool(tool, args)

    anyio.run(go)
    return holder["out"]


def _poll(job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = gate_runs.gate_run_status(job_id)
        if out["status"] not in ("running", "attaching"):
            return out
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# ── CLI ─────────────────────────────────────────────────────────────────────────
def test_cli_engine_flag(store: Path, fake: _Fake, capsys) -> None:
    from rebar._cli._llm_commands import _review_plan

    tid = _ticket(store)
    assert _review_plan([tid, "--engine", "agentic", "-o", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["coverage"]["engine"] == "agentic"

    tid2 = _ticket(store)
    assert _review_plan([tid2, "-o", "json"]) == 0
    assert "engine" not in json.loads(capsys.readouterr().out)["coverage"]

    with pytest.raises(SystemExit) as raised:
        _review_plan([tid, "--engine", "bogus"])
    assert raised.value.code == 2


# ── MCP sync ────────────────────────────────────────────────────────────────────
def test_mcp_sync_engine_parameter(store: Path, fake: _Fake) -> None:
    mcp = _server()
    out = _call(mcp, "review_plan", {"ticket_id": _ticket(store), "engine": "agentic"})
    assert out["coverage"]["engine"] == "agentic"

    calls = fake.calls
    with pytest.raises(Exception) as raised:
        _call(mcp, "review_plan", {"ticket_id": _ticket(store), "engine": "bogus"})
    assert "bogus" in str(raised.value)
    assert fake.calls == calls


# ── MCP async start + poll ──────────────────────────────────────────────────────
@pytest.mark.parametrize("engine", ["agentic", "pipeline"])
def test_mcp_async_start_runs_the_selected_engine(store: Path, fake: _Fake, engine: str) -> None:
    from rebar.llm.plan_review import sidecar

    tid = _ticket(store)
    started = _call(_server(), "review_plan_start", {"ticket_id": tid, "engine": engine})
    out = _poll(started["job_id"])
    assert out["status"] == "passed", out
    assert out["findings"]["readable"] is True, out
    stored = sidecar.latest_review_result(tid, repo_root=str(store))
    assert (stored["coverage"].get("engine") or "pipeline") == engine
    status = rebar.llm.plan_review_status(tid, repo_root=str(store))
    assert (status["verdict"], status["engine"]) == ("certified", engine)


def test_mcp_async_start_refuses_an_unknown_engine_before_any_job(store: Path, fake) -> None:
    index_before = sorted(p.name for p in store.rglob("*gate*run*"))
    with pytest.raises(Exception) as raised:
        _call(_server(), "review_plan_start", {"ticket_id": _ticket(store), "engine": "bogus"})
    assert "bogus" in str(raised.value)
    assert sorted(p.name for p in store.rglob("*gate*run*")) == index_before
    assert fake.calls == 0


# ── dedup key ───────────────────────────────────────────────────────────────────
def test_dedup_key_carries_the_resolved_engine(store: Path) -> None:
    tid = _ticket(store)
    pipeline = _mcp_llm._plan_review_variant(tid, None, _mcp_llm._resolve_engine(None))
    assert pipeline == _mcp_llm._plan_review_variant(tid, None, "pipeline")
    assert pipeline != _mcp_llm._plan_review_variant(tid, None, "agentic")
    with pytest.raises(ConfigError, match="bogus"):
        _mcp_llm._resolve_engine("bogus")


def test_same_engine_starts_share_a_job_and_engines_do_not(store: Path, monkeypatch) -> None:
    release = threading.Event()

    def held(ticket_id, **kw):
        release.wait(timeout=30)
        return {"verdict": "PASS", "ticket_id": ticket_id}

    monkeypatch.setattr(rebar.llm, "review_plan", held)
    mcp = _server()
    tid = _ticket(store)
    try:
        first = _call(mcp, "review_plan_start", {"ticket_id": tid, "engine": "agentic"})
        second = _call(mcp, "review_plan_start", {"ticket_id": tid, "engine": "agentic"})
        other = _call(mcp, "review_plan_start", {"ticket_id": tid, "engine": "pipeline"})
    finally:
        release.set()
    assert first["job_id"] == second["job_id"]
    assert other["job_id"] != first["job_id"]
    for handle in (first, other):
        _poll(handle["job_id"])


# ── congestion is identical under both engines ──────────────────────────────────
def test_congestion_is_identical_under_both_engines(store: Path, monkeypatch, capsys) -> None:
    from rebar._cli._llm_commands import _review_plan

    def congested(*args, **kwargs):
        raise ga.GateCongestedError("plan_review", 2)

    monkeypatch.setattr(rebar.llm, "review_plan", congested)
    payloads = [
        _mcp_llm._review_plan_body("t-1", None, None, False, readonly=True, engine=e)
        for e in ("pipeline", "agentic")
    ]
    assert payloads[0] == payloads[1]
    assert payloads[0]["error"] == "gate_congested" and payloads[0]["retryable"] is True
    codes = [_review_plan(["t-1", "--engine", e]) for e in ("pipeline", "agentic")]
    assert codes == [11, 11]
    capsys.readouterr()
    mcp = _server()
    tid = _ticket(store)
    outs = []
    for engine in ("pipeline", "agentic"):
        handle = _call(mcp, "review_plan_start", {"ticket_id": tid, "engine": engine})
        outs.append(_poll(handle["job_id"]))
    assert [o["status"] for o in outs] == ["failed", "failed"]
    assert outs[0].get("error") == outs[1].get("error")
