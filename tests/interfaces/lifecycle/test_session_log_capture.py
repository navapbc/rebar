"""e7e4 (epic 7738): the session-log capture helper across library, CLI, and MCP.

`append_session_log` creates a `session_log` on first use (titled by `summary`)
and appends entries to the SAME log on subsequent calls; `start` rotates to a
fresh log. The "current" log is tracked by a local `.rebar/current_session_log`
pointer, so the three interfaces — sharing one checkout — converge on one log.
All writes go through the locked seam, inheriting add5's rules (blocking links
refused; relates_to / discovered_from allowed). The MCP tool is write-gated.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import RebarError

pytestmark = pytest.mark.interface


def _cli(repo: Path, *args: str, readonly: bool = False) -> tuple[int, str, str]:
    env = {**os.environ, "REBAR_ROOT": str(repo)}
    if readonly:
        env["REBAR_MCP_READONLY"] = "1"
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


# ── create-then-append (library) ───────────────────────────────────────────────
def test_first_call_creates_then_appends_same_log(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    a = rebar.append_session_log("entry one", summary="My session", repo_root=r)
    b = rebar.append_session_log("entry two", repo_root=r)
    assert a["created"] is True and b["created"] is False
    assert a["id"] == b["id"], "subsequent calls append to the same log, not a new one"
    state = rebar.show_ticket(a["id"], repo_root=r)
    assert state["ticket_type"] == "session_log"
    assert state["title"] == "My session"
    bodies = [c.get("body") for c in state.get("comments", [])]
    assert "entry one" in bodies and "entry two" in bodies


def test_exactly_one_log_created_on_first_use(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    rebar.append_session_log("e1", repo_root=r)
    rebar.append_session_log("e2", repo_root=r)
    rebar.append_session_log("e3", repo_root=r)
    logs = rebar.list_tickets(ticket_type="session_log", repo_root=r)
    assert len(logs) == 1, "create-on-first-use must not recreate on every append"


def test_start_rotates_to_fresh_log(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    first = rebar.append_session_log("e1", repo_root=r)["id"]
    rotated = rebar.start_session_log(summary="second session", repo_root=r)["id"]
    assert rotated != first
    after = rebar.append_session_log("e2", repo_root=r)
    assert after["id"] == rotated and after["created"] is False


def test_empty_entry_refused(rebar_repo: Path) -> None:
    with pytest.raises(RebarError):
        rebar.append_session_log("", repo_root=str(rebar_repo))


# ── linking rules (relates_to allowed, blocks refused) ──────────────────────────
def test_relates_to_allowed_blocking_refused(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    task = rebar.create_ticket("task", "Real work", description="body", repo_root=r)
    log = rebar.append_session_log("documenting", relates_to=task, repo_root=r)
    deps = rebar.deps(log["id"], repo_root=r)
    assert any(d.get("relation") == "relates_to" for d in deps.get("deps", []))
    with pytest.raises(RebarError, match="session_log"):
        rebar.link(log["id"], task, "blocks", repo_root=r)


# ── three-interface parity (one checkout → one current log) ─────────────────────
def test_library_cli_mcp_share_current_log(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    # library creates the current log
    lib = rebar.append_session_log("from library", summary="shared", repo_root=r)
    log_id = lib["id"]

    # CLI appends to the SAME current log (reads the local pointer)
    rc, out, err = _cli(rebar_repo, "session-log", "append", "from cli")
    assert rc == 0, err
    assert json.loads(out)["id"] == log_id

    # MCP appends to the SAME current log
    from rebar.mcp_server import build_server

    res = asyncio.run(build_server().call_tool("log_session", {"entry": "from mcp"}))
    structured = getattr(res, "structured_content", None)
    if structured is None and isinstance(res, tuple):
        structured = res[1]
    inner = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert inner["id"] == log_id

    bodies = [c.get("body") for c in rebar.show_ticket(log_id, repo_root=r).get("comments", [])]
    assert {"from library", "from cli", "from mcp"} <= set(bodies)


def test_cli_start_then_append(rebar_repo: Path) -> None:
    rc, out, err = _cli(rebar_repo, "session-log", "start", "--summary=cli session")
    assert rc == 0, err
    sid = json.loads(out)["id"]
    rc, out, err = _cli(rebar_repo, "session-log", "append", "logged")
    assert rc == 0, err
    assert json.loads(out)["id"] == sid


def test_cli_unknown_action_is_usage_error(rebar_repo: Path) -> None:
    rc, out, err = _cli(rebar_repo, "session-log", "frobnicate")
    assert rc == 1
    assert "Usage" in err


# ── MCP write-gating ────────────────────────────────────────────────────────────
def test_mcp_log_session_present_when_writable(rebar_repo: Path) -> None:
    from rebar.mcp_server import build_server

    names = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "log_session" in names


def test_mcp_log_session_absent_under_readonly(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    from rebar.mcp_server import build_server

    names = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "log_session" not in names, "write-gated tool must not register under readonly"
