"""Held-out edge and compatibility oracle for programmatic bridge operations."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from adapters import _unwrap

import rebar

pytestmark = pytest.mark.unit


def _tool_map():
    from rebar.mcp_server import build_server

    return {tool.name: tool for tool in asyncio.run(build_server().list_tools())}


class _Registrar:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


def test_new_library_signatures_are_explicit_and_legacy_reconcile_is_absent() -> None:
    assert str(inspect.signature(rebar.bridge_preview)) == (
        "(*, only: 'list[str] | None' = None, exclude: 'list[str] | None' = None, "
        "repo_root=None) -> 'BridgeRun'"
    )
    assert str(inspect.signature(rebar.bridge_run)) == (
        "(profile: 'str | None' = None, *, repo_root=None) -> 'BridgeRun'"
    )
    assert str(inspect.signature(rebar.bridge_sync)) == (
        "(*, only: 'list[str] | None' = None, exclude: 'list[str] | None' = None, "
        "max_changes: 'int | None' = None, repo_root=None) -> 'BridgeRun'"
    )
    assert str(inspect.signature(rebar.bridge_status)) == (
        "(*, target_environment_id: 'str | None' = None, "
        "max_age_seconds: 'int | None' = None, repo_root=None) -> 'BridgeStatus'"
    )
    assert str(inspect.signature(rebar.bridge_pause)) == (
        "(reason: 'str', *, repo_root=None) -> 'BridgeControl'"
    )
    assert str(inspect.signature(rebar.bridge_resume)) == "(*, repo_root=None) -> 'BridgeControl'"
    assert str(inspect.signature(rebar.bridge_check_access)) == "() -> 'BridgeAccessCheck'"

    assert not hasattr(rebar, "reconcile")
    assert str(inspect.signature(rebar.bridge_fsck)) == "(*, repo_root=None) -> 'BridgeFsck'"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: rebar.bridge_preview(only=["a"], exclude=["b"]), "mutually exclusive"),
        (lambda: rebar.bridge_sync(max_changes=0), "positive"),
        (lambda: rebar.bridge_sync(max_changes=True), "positive"),
        (lambda: rebar.bridge_status(max_age_seconds=0), "positive"),
        (lambda: rebar.bridge_pause(""), "reason"),
    ],
)
def test_library_validates_inputs_before_operational_work(call, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        call()


def test_new_mcp_input_schemas_are_explicit_and_mode_free() -> None:
    tools = _tool_map()
    assert set(tools["bridge_preview"].inputSchema["properties"]) == {"only", "exclude"}
    assert set(tools["bridge_run"].inputSchema["properties"]) == {"profile"}
    assert set(tools["bridge_sync"].inputSchema["properties"]) == {
        "only",
        "exclude",
        "max_changes",
    }
    assert set(tools["bridge_status"].inputSchema["properties"]) == {
        "target_environment_id",
        "max_age_seconds",
    }
    assert set(tools["bridge_pause"].inputSchema["properties"]) == {"reason"}
    assert tools["bridge_pause"].inputSchema["required"] == ["reason"]
    assert tools["bridge_resume"].inputSchema.get("properties", {}) == {}
    assert tools["bridge_check_access"].inputSchema.get("properties", {}) == {}
    for name in (
        "bridge_preview",
        "bridge_status",
        "bridge_pause",
        "bridge_resume",
        "bridge_check_access",
    ):
        assert "mode" not in tools[name].inputSchema.get("properties", {})


@pytest.mark.parametrize("name", ["bridge_run", "bridge_sync", "bridge_pause", "bridge_resume"])
def test_mutating_mcp_tools_are_gated_before_the_library_call(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar.mcp_server import build_server

    calls: list[dict] = []
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.delenv("REBAR_MCP_ALLOW_JIRA_SYNC", raising=False)
    monkeypatch.setattr(rebar, name, lambda **kwargs: calls.append(kwargs) or {})
    arguments = {
        "bridge_run": {"profile": "live"},
        "bridge_sync": {},
        "bridge_pause": {"reason": "maintenance"},
        "bridge_resume": {},
    }[name]

    with pytest.raises(Exception, match="disabled"):
        asyncio.run(build_server().call_tool(name, arguments))
    assert calls == []


def test_bridge_mutation_gates_accept_legacy_boolean_context_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extracted registrars remain compatible with pre-helper test/app contexts."""
    from rebar import _mcp_reads

    registrar = _Registrar()
    ctx = SimpleNamespace(readonly=False, allow_jira_sync=False)
    _mcp_reads.register_bridge_tools(registrar, ctx)

    with pytest.raises(ValueError, match="disabled"):
        registrar.tools["bridge_sync"]()

    ctx.allow_jira_sync = True
    monkeypatch.setattr(
        rebar,
        "bridge_sync",
        lambda **_kwargs: {
            "route": "sync",
            "state": "converged",
            "returncode": 0,
            "details": {},
        },
    )
    result = registrar.tools["bridge_sync"]()
    assert result.model_dump() == {
        "route": "sync",
        "state": "converged",
        "returncode": 0,
        "details": {},
    }


def test_read_only_mcp_bridge_tools_are_not_blocked_by_jira_sync_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar.mcp_server import build_server

    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.delenv("REBAR_MCP_ALLOW_JIRA_SYNC", raising=False)
    expected = {
        "bridge_preview": {
            "route": "preview",
            "state": "converged",
            "returncode": 0,
            "details": {},
        },
        "bridge_status": {"verdict": "NEVER_RUN", "target_environment_id": "worker-a"},
        "bridge_check_access": {"verdict": "PASS", "steps": []},
    }
    for name, value in expected.items():
        monkeypatch.setattr(rebar, name, lambda value=value, **_kwargs: value)

    server = build_server()
    arguments = {
        "bridge_preview": {},
        "bridge_status": {"target_environment_id": "worker-a"},
        "bridge_check_access": {},
    }
    for name, value in expected.items():
        assert _unwrap(asyncio.run(server.call_tool(name, arguments[name]))) == value


def test_bridge_result_schemas_are_wired_to_public_types_and_mcp_models() -> None:
    from rebar import schemas
    from rebar._mcp_models import (
        BridgeAccessCheckOut,
        BridgeControlOut,
        BridgeRunOut,
        BridgeStatusOut,
    )

    samples = {
        schemas.BRIDGE_RUN: (
            BridgeRunOut,
            {"route": "preview", "state": "converged", "returncode": 0, "details": {}},
        ),
        schemas.BRIDGE_STATUS: (
            BridgeStatusOut,
            {"verdict": "HEALTHY", "target_environment_id": "worker-a"},
        ),
        schemas.BRIDGE_CONTROL: (BridgeControlOut, {"state": "resumed"}),
        schemas.BRIDGE_ACCESS_CHECK: (
            BridgeAccessCheckOut,
            {"verdict": "PASS", "steps": [{"step": "STEP_CREATE", "passed": True}]},
        ),
    }
    for schema_name, (model, sample) in samples.items():
        schemas.validator(schema_name).validate(sample)
        assert model.model_validate(sample).model_dump(exclude_none=True) == sample
