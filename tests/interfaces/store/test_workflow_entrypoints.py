"""WS-C4: run_workflow + status/result across library / CLI / MCP.

Uses agent workflows with ``dry_run=True`` (the offline FakeRunner — no tokens, no
WS-D runner needed) so the whole entrypoint surface is exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import rebar

pytest.importorskip("jsonschema")

from adapters import _unwrap  # noqa: E402  (tests/interfaces on sys.path)

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


def test_mcp_run_workflow_is_async_and_pollable(rebar_repo: Path) -> None:
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

    # Poll get_workflow_status until the background run settles. Pass ticket_id (the
    # run's durable home) so the poll doesn't depend on the process-global run-index
    # — run_id-only lookup is covered by test_library_run_status_result.
    final = None
    for _ in range(50):
        st = _unwrap(
            asyncio.run(srv.call_tool("get_workflow_status", {"run_id": rid, "ticket_id": tid}))
        )
        if st.get("status") in ("succeeded", "failed"):
            final = st
            break
        time.sleep(0.1)
    assert final is not None, "background run never settled"
    assert final["status"] == "succeeded"

    rr = _unwrap(
        asyncio.run(srv.call_tool("get_workflow_result", {"run_id": rid, "ticket_id": tid}))
    )
    assert rr["terminal_step"] == "review"
