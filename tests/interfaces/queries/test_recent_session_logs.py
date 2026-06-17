"""e2e3 (epic 7738): the "recent session logs" read across library, CLI, and MCP.

`recent_session_logs` returns the newest `session_log` tickets (by `created_at`
ns, descending), default limit 5. It is the one read that surfaces session_logs
by type (they are hidden from default `list`). The library impl auto-flows to CLI
(`rebar session-logs`) and the MCP tool (`recent_session_logs`); this asserts
ordering, the limit, the empty-store case, and three-interface parity.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.interface


def _ids(rows: list[dict]) -> list[str]:
    return [r["ticket_id"] for r in rows]


def _make_logs(repo: Path, n: int) -> list[str]:
    """Create n session logs in order; return ids oldest→newest."""
    r = str(repo)
    return [
        rebar.create_ticket("session_log", f"Log {i}", description=f"body {i}", repo_root=r)
        for i in range(n)
    ]


def _cli_session_logs(repo: Path, *args: str) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "session-logs", *args, "--output", "json"],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    assert cp.returncode == 0, f"cli failed (rc={cp.returncode}): {cp.stderr}"
    return json.loads(cp.stdout)


def _mcp_session_logs(repo: Path, **kwargs) -> list[dict]:
    from rebar.mcp_server import build_server

    srv = build_server()
    result = asyncio.run(srv.call_tool("recent_session_logs", kwargs))
    # FastMCP returns (content, structured) or a CallToolResult; normalize.
    structured = getattr(result, "structured_content", None)
    if structured is None and isinstance(result, tuple):
        structured = result[1]
    # structured is typically {"result": [...]} for a list return.
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    return structured  # already a list


# ── ordering + limit (library) ─────────────────────────────────────────────────
def test_newest_first_and_default_limit_five(rebar_repo: Path) -> None:
    logs = _make_logs(rebar_repo, 7)  # oldest→newest
    got = _ids(rebar.recent_session_logs(repo_root=str(rebar_repo)))
    assert len(got) == 5, "default limit is 5"
    assert got == list(reversed(logs))[:5], "newest-first, capped at 5"


def test_custom_limit(rebar_repo: Path) -> None:
    logs = _make_logs(rebar_repo, 4)
    got = _ids(rebar.recent_session_logs(limit=2, repo_root=str(rebar_repo)))
    assert got == list(reversed(logs))[:2]


def test_limit_exceeding_count_returns_all(rebar_repo: Path) -> None:
    logs = _make_logs(rebar_repo, 3)
    got = _ids(rebar.recent_session_logs(limit=100, repo_root=str(rebar_repo)))
    assert got == list(reversed(logs))


def test_empty_store_returns_empty(rebar_repo: Path) -> None:
    # Non-log tickets present, but no session_logs.
    rebar.create_ticket("task", "a task", description="body", repo_root=str(rebar_repo))
    assert rebar.recent_session_logs(repo_root=str(rebar_repo)) == []


def test_non_positive_limit_returns_empty(rebar_repo: Path) -> None:
    _make_logs(rebar_repo, 2)
    assert rebar.recent_session_logs(limit=0, repo_root=str(rebar_repo)) == []


# ── three-interface parity ──────────────────────────────────────────────────────
def test_library_cli_mcp_agree(rebar_repo: Path) -> None:
    _make_logs(rebar_repo, 6)
    lib = _ids(rebar.recent_session_logs(limit=3, repo_root=str(rebar_repo)))
    cli = _ids(_cli_session_logs(rebar_repo, "--limit=3"))
    mcp = _ids(_mcp_session_logs(rebar_repo, limit=3))
    assert lib == cli == mcp, f"interfaces disagree: lib={lib} cli={cli} mcp={mcp}"
    assert len(lib) == 3
