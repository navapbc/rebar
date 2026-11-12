"""WS-C4: run_workflow + status/result across library / CLI / MCP.

Uses agent workflows with ``dry_run=True`` (the offline FakeRunner — no tokens, no
WS-D runner needed) so the whole entrypoint surface is exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar

pytest.importorskip("jsonschema")

from adapters import _unwrap

AGENT_WF = {
    "schema_version": "1",
    "name": "entry_demo",
    "steps": [{"id": "review", "prompt": "code_quality", "mode": "findings"}],
}


# ── library ───────────────────────────────────────────────────────────────────


def test_library_run_status_result(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Target", repo_root=r)
    res = rebar.run_workflow(AGENT_WF, ticket_id=tid, dry_run=True, repo_root=r)
    assert res["status"] == "succeeded"
    rid = res["run_id"]
    # status by run_id alone (resolved via the local run index)
    st = rebar.get_workflow_status(rid, repo_root=r)
    assert st["status"] == "succeeded"
    assert st["steps"]["review"] == "succeeded"
    rr = rebar.get_workflow_result(rid, repo_root=r)
    assert rr["terminal_step"] == "review"
    assert rr["terminal_output"]["_fake"] is True


def test_library_run_by_name(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    wf_dir = rebar_repo / ".rebar" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "named.yaml").write_text(
        'schema_version: "1"\nname: named\nsteps:\n'
        "  - id: review\n    prompt: code_quality\n    mode: findings\n"
    )
    res = rebar.run_workflow("named", dry_run=True, repo_root=r)
    assert res["status"] == "succeeded"
    assert res["workflow_name"] == "named"


def test_status_unknown_run_id_errors(rebar_repo: Path) -> None:
    from rebar.llm.errors import WorkflowError

    with pytest.raises(WorkflowError, match="unknown run_id"):
        rebar.get_workflow_status("no-such-run", repo_root=str(rebar_repo))


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        check=False,
    )


def test_cli_run_then_status(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Target", repo_root=r)
    wf = rebar_repo / "wf.yaml"
    wf.write_text(
        'schema_version: "1"\nname: clidemo\nsteps:\n'
        "  - id: review\n    prompt: code_quality\n    mode: findings\n"
    )
    cp = _cli(
        rebar_repo, "workflow", "run", str(wf), "--ticket", tid, "--dry-run", "--output", "json"
    )
    assert cp.returncode == 0, cp.stderr
    res = json.loads(cp.stdout)
    assert res["status"] == "succeeded"
    rid = res["run_id"]
    cp2 = _cli(rebar_repo, "workflow", "status", rid, "--ticket", tid, "--output", "json")
    assert cp2.returncode == 0, cp2.stderr
    st = json.loads(cp2.stdout)
    assert st["status"] == "succeeded"


# ── MCP (async) ───────────────────────────────────────────────────────────────


def _capture_run_threads(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record the background run thread(s) run_workflow spawns, so a test can wait on the
    run's own completion instead of a fixed wall-clock window (weakwilled-catchable-cattle)."""
    from rebar import _mcp_writes

    threads: list = []
    real = _mcp_writes.spawn_context_daemon

    def spy(target, *, name=None):
        thread = real(target, name=name)
        threads.append(thread)
        return thread

    monkeypatch.setattr(_mcp_writes, "spawn_context_daemon", spy)
    return threads


#: Upper bound on waiting for the background run to finish. It bounds a hung run, not a slow
#: one: the run normally finishes in well under a second.
_RUN_JOIN_BOUND_S = 120


def test_mcp_run_workflow_is_async_and_pollable(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.mcp_server import build_server

    r = str(rebar_repo)
    tid = rebar.create_ticket("task", "Target", repo_root=r)
    wf = rebar_repo / ".rebar" / "workflows" / "mcpdemo.yaml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(
        'schema_version: "1"\nname: mcpdemo\nsteps:\n'
        "  - id: review\n    prompt: code_quality\n    mode: findings\n"
    )
    srv = build_server()
    threads = _capture_run_threads(monkeypatch)

    # run_workflow returns a run_id IMMEDIATELY (status 'running'); the run executes
    # in the background.
    started = _unwrap(
        asyncio.run(
            srv.call_tool(
                "run_workflow",
                {"workflow": "mcpdemo", "ticket_id": tid, "dry_run": True},
            )
        )
    )
    assert started["status"] == "running"
    rid = started["run_id"]

    # Wait on the run's own completion signal (its thread), then read the settled state
    # through the poll surface. Pass ticket_id (the run's durable home) so the poll doesn't
    # depend on the process-global run-index — run_id-only lookup is covered by
    # test_library_run_status_result.
    assert len(threads) == 1, threads
    threads[0].join(timeout=_RUN_JOIN_BOUND_S)
    assert not threads[0].is_alive(), "background run never settled"
    final = _unwrap(
        asyncio.run(srv.call_tool("get_workflow_status", {"run_id": rid, "ticket_id": tid}))
    )
    assert final["status"] == "succeeded", final

    rr = _unwrap(
        asyncio.run(srv.call_tool("get_workflow_result", {"run_id": rid, "ticket_id": tid}))
    )
    assert rr["terminal_step"] == "review"
