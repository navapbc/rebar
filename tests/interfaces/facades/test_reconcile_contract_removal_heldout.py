"""Held-out oracle for removing Python/MCP reconcile compatibility contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from adapters import _unwrap

import rebar
from rebar import _lib_ops
from rebar.mcp_server import build_server

pytestmark = pytest.mark.unit


def _tool_map():
    return {tool.name: tool for tool in asyncio.run(build_server().list_tools())}


def test_python_reconcile_call_rejects_before_operational_work(monkeypatch) -> None:
    calls: list[object] = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("removed rebar.reconcile compatibility reached subprocess work")

    monkeypatch.setattr(_lib_ops, "subprocess", SimpleNamespace(run=fail_if_called))

    with pytest.raises(AttributeError):
        rebar.reconcile("dry-run", repo_root="/tmp/unused")
    assert calls == []


def test_mcp_reconcile_tool_rejects_before_operational_work(monkeypatch) -> None:
    calls: list[str] = []

    def fail_if_called(mode: str = "dry-run") -> dict:
        calls.append(mode)
        pytest.fail("removed MCP reconcile tool reached library work")

    monkeypatch.setattr(rebar, "reconcile", fail_if_called, raising=False)

    assert "reconcile" not in _tool_map()
    with pytest.raises(Exception, match="reconcile"):
        asyncio.run(build_server().call_tool("reconcile", {"mode": "dry-run"}))
    assert calls == []


def test_explicit_bridge_mcp_result_contracts_survive_reconcile_removal(monkeypatch) -> None:
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setattr(
        rebar,
        "bridge_preview",
        lambda **_kwargs: {
            "route": "preview",
            "state": "converged",
            "returncode": 0,
            "details": {"plan": []},
        },
    )
    monkeypatch.setattr(
        rebar,
        "bridge_sync",
        lambda **_kwargs: {
            "route": "sync",
            "state": "converged",
            "returncode": 0,
            "details": {"applied": 0},
        },
    )
    monkeypatch.setattr(
        rebar,
        "bridge_run",
        lambda **_kwargs: {
            "route": "run",
            "state": "converged",
            "returncode": 0,
            "details": {"profile": "dry-run"},
        },
    )
    monkeypatch.setattr(
        rebar,
        "bridge_fsck",
        lambda **_kwargs: {"unknown_event_types": [], "binding_drift": {}, "store_integrity": []},
    )
    monkeypatch.setattr(
        rebar,
        "bridge_status",
        lambda **_kwargs: {"verdict": "NEVER_RUN", "target_environment_id": "worker-a"},
    )

    server = build_server()
    cases = {
        "bridge_preview": ({}, {"route": "preview", "state": "converged", "returncode": 0}),
        "bridge_sync": ({}, {"route": "sync", "state": "converged", "returncode": 0}),
        "bridge_run": ({"profile": "dry-run"}, {"route": "run", "state": "converged"}),
        "bridge_fsck": ({}, {"unknown_event_types": [], "binding_drift": {}}),
        "bridge_status": (
            {"target_environment_id": "worker-a"},
            {"verdict": "NEVER_RUN", "target_environment_id": "worker-a"},
        ),
    }

    for tool_name, (arguments, expected_subset) in cases.items():
        payload = _unwrap(asyncio.run(server.call_tool(tool_name, arguments)))
        assert expected_subset.items() <= payload.items()


def test_mcp_jira_sync_env_help_names_bridge_sync_not_removed_reconcile() -> None:
    from rebar.mcp_server import MCP_ENV_VARS

    help_text = next(
        v["description"] for v in MCP_ENV_VARS if v["name"] == "REBAR_MCP_ALLOW_JIRA_SYNC"
    )

    assert "bridge_sync" in help_text
    assert "reconcile" not in help_text.lower()


def test_mcp_server_docstring_no_registered_reconcile_tool_prose() -> None:
    import rebar.mcp_server as mcp_server

    doc = mcp_server.__doc__ or ""

    assert "``reconcile``" not in doc
    assert "registered MCP reconcile" not in doc.lower()


def test_mcp_server_comments_no_legacy_reconcile_tool_prose() -> None:
    source = Path("src/rebar/mcp_server.py").read_text(encoding="utf-8")

    stale_needles = [
        "The reconcile tool gates modes",
        "reconcile is dry-run only",
        "``reconcile`` defaults",
    ]
    for needle in stale_needles:
        assert needle not in source
