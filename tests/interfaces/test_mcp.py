"""MCP-server-specific behaviors (FastMCP).

Covers the read-only gate, the live-reconcile gate, and the lazy-import error
when the optional `mcp` extra is absent. Skipped wholesale if `mcp` is not
installed.
"""

from __future__ import annotations

import builtins

import pytest

pytest.importorskip("mcp")

import asyncio

from rebar.mcp_server import build_server


def _tool_names(srv) -> set[str]:
    return {t.name for t in asyncio.run(srv.list_tools())}


def test_readonly_hides_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    names = _tool_names(build_server())
    # Reads remain; writes are gone.
    assert "show_ticket" in names and "list_tickets" in names
    for write_tool in (
        "create_ticket", "transition_ticket", "tag_ticket", "archive_ticket",
        "claim_ticket", "reopen_ticket", "set_file_impact", "set_verify_commands",
    ):
        assert write_tool not in names, write_tool
    # WS5d: quality-gate + file-impact READ tools stay exposed in readonly mode.
    for read_tool in (
        "clarity_check", "check_ac", "quality_check", "validate",
        "get_file_impact", "get_verify_commands",
    ):
        assert read_tool in names, read_tool


def test_write_tools_present_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    names = _tool_names(build_server())
    assert {
        "create_ticket", "transition_ticket", "claim_ticket", "reopen_ticket",
        "set_file_impact", "set_verify_commands",
    } <= names


def test_live_reconcile_refused_without_optin(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    monkeypatch.delenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", raising=False)
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": "live"}))
    assert "live reconcile is disabled" in str(exc.value).lower()


def test_absent_mcp_extra_raises_systemexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the optional `mcp` extra is missing, build_server() exits with a clear
    install hint (SystemExit), not an opaque ImportError."""
    import rebar.mcp_server as m

    real_import = builtins.__import__

    def _fail_mcp(name, *a, **k):
        if name == "mcp.server.fastmcp" or name.startswith("mcp.server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fail_mcp)
    with pytest.raises(SystemExit) as exc:
        m.build_server()
    assert "mcp" in str(exc.value).lower()
