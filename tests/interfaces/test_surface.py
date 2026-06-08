"""Capability-surface assertions.

Encodes the agreed per-interface operation surface (the Part 2 matrix) so drift
in either direction is caught: the documented exceptions (MCP has no `init`; no
interface exposes the removed `classify` or any brainstorm surface) and the MCP
parity additions (assignee on create, list filters, compact, fsck).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest


def _mcp_tools() -> dict:
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    srv = build_server()
    tools = asyncio.run(srv.list_tools())
    return {t.name: t for t in tools}


def _tool_params(tool) -> set[str]:
    schema = tool.inputSchema or {}
    return set((schema.get("properties") or {}).keys())


# ── MCP surface ───────────────────────────────────────────────────────────────
def test_mcp_exposes_expected_tools() -> None:
    names = set(_mcp_tools())
    expected = {
        "show_ticket", "list_tickets", "ticket_deps", "ready_tickets",
        "next_batch", "reconcile", "create_ticket", "transition_ticket",
        "comment_ticket", "edit_ticket", "link_tickets", "unlink_tickets",
        "tag_ticket", "untag_ticket", "archive_ticket",
        "compact_ticket", "fsck",
    }
    assert expected <= names, f"missing MCP tools: {expected - names}"


def test_mcp_has_no_init_or_classify() -> None:
    """Documented exception: MCP has no init (operator bootstrap); classify was
    removed entirely (DSO agent-routing)."""
    names = set(_mcp_tools())
    assert "init" not in names
    assert "classify" not in names
    assert not any("brainstorm" in n for n in names)


def test_mcp_create_accepts_assignee() -> None:
    assert "assignee" in _tool_params(_mcp_tools()["create_ticket"])


def test_mcp_list_accepts_all_filters() -> None:
    params = _tool_params(_mcp_tools()["list_tickets"])
    assert {"priority", "parent", "without_tag"} <= params


# ── Library surface ───────────────────────────────────────────────────────────
def test_library_public_api() -> None:
    import rebar

    for fn in (
        "init_repo", "create_ticket", "transition", "comment", "edit_ticket",
        "link", "unlink", "tag", "untag", "archive", "compact", "fsck",
        "show_ticket", "list_tickets", "deps", "ready", "next_batch", "reconcile",
    ):
        assert callable(getattr(rebar, fn)), fn
    assert not hasattr(rebar, "classify")


# ── CLI surface ───────────────────────────────────────────────────────────────
def _cli_usage() -> str:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "no-such-subcommand"],
        capture_output=True, text=True,
    )
    return (cp.stdout + cp.stderr)


def test_cli_usage_has_no_classify() -> None:
    usage = _cli_usage()
    assert "classify" not in usage
    assert "create" in usage and "list" in usage


def test_cli_rejects_classify() -> None:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "classify"],
        capture_output=True, text=True,
    )
    assert cp.returncode != 0
    assert "unknown subcommand" in (cp.stdout + cp.stderr).lower()
