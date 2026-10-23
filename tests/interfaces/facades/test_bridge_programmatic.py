"""Happy-path contracts for the additive library and MCP bridge surfaces."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
from pathlib import Path

import pytest
from adapters import _unwrap

import rebar

pytestmark = pytest.mark.unit

LAST_PASS_REF = "refs/reconciler/last-pass"

_NEW_TOOLS = {
    "bridge_preview",
    "bridge_run",
    "bridge_sync",
    "bridge_status",
    "bridge_pause",
    "bridge_resume",
    "bridge_check_access",
}


def _plant_blob(repo: Path, ref: str, payload: dict) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    oid = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, oid],
        capture_output=True,
        check=True,
    )


def test_public_library_exports_explicit_bridge_operations_only() -> None:
    for name in sorted(_NEW_TOOLS | {"bridge_fsck"}):
        assert callable(getattr(rebar, name)), name
    assert _NEW_TOOLS <= set(rebar.__all__)
    assert "reconcile" not in rebar.__all__
    assert not hasattr(rebar, "reconcile")


def test_bridge_status_reads_the_durable_snapshot(rebar_repo: Path) -> None:
    _plant_blob(
        rebar_repo,
        LAST_PASS_REF,
        {
            "schema_version": 1,
            "pass_id": "programmatic-happy",
            "environment_id": "worker-a",
            "outcome": "success",
            "completed_at": "2026-08-09T12:00:00Z",
            "lock_fence": 4,
        },
    )

    result = rebar.bridge_status(
        target_environment_id="worker-a",
        repo_root=rebar_repo,
    )

    assert result["verdict"] == "HEALTHY"
    assert result["pass_id"] == "programmatic-happy"
    assert result["target_environment_id"] == "worker-a"
    assert result["lock_fence"] == 4


def test_mcp_registers_typed_bridge_tools_without_legacy_reconcile() -> None:
    from rebar.mcp_server import build_server

    tools = {tool.name: tool for tool in asyncio.run(build_server().list_tools())}
    assert _NEW_TOOLS | {"bridge_fsck"} <= set(tools)
    assert "reconcile" not in tools
    for name in _NEW_TOOLS:
        assert tools[name].outputSchema, name
    assert set(tools["bridge_run"].inputSchema.get("properties", {})) == {"profile"}
    for name in _NEW_TOOLS - {"bridge_run"}:
        assert "mode" not in tools[name].inputSchema.get("properties", {})


def test_mcp_bridge_tools_return_the_public_library_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar.mcp_server import build_server

    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")

    expected = {
        "bridge_preview": {
            "route": "preview",
            "state": "converged",
            "returncode": 0,
            "details": {"mutation_count": 2, "no_write": True},
        },
        "bridge_run": {
            "route": "run",
            "state": "converged",
            "returncode": 0,
            "details": {"profile": "live", "delivery_attempted": True},
        },
        "bridge_sync": {
            "route": "sync",
            "state": "converged",
            "returncode": 0,
            "details": {"mutation_count": 2, "mutations_applied": 2},
        },
        "bridge_status": {
            "verdict": "HEALTHY",
            "target_environment_id": "worker-a",
            "pass_id": "p1",
        },
        "bridge_pause": {
            "state": "paused",
            "reason": "maintenance",
            "who": "ops@example.com",
            "paused_at": "2026-08-09T12:00:00Z",
        },
        "bridge_resume": {"state": "resumed"},
        "bridge_check_access": {
            "verdict": "PASS",
            "steps": [{"step": "STEP_CREATE", "passed": True}],
        },
    }
    seen: dict[str, dict] = {}

    def fake(name: str):
        def call(**kwargs):
            seen[name] = kwargs
            return expected[name]

        return call

    for name in _NEW_TOOLS:
        monkeypatch.setattr(rebar, name, fake(name))

    server = build_server()
    calls = {
        "bridge_preview": {"only": ["ticket-a"]},
        "bridge_run": {"profile": "live"},
        "bridge_sync": {"exclude": ["ticket-b"], "max_changes": 10},
        "bridge_status": {"target_environment_id": "worker-a", "max_age_seconds": 60},
        "bridge_pause": {"reason": "maintenance"},
        "bridge_resume": {},
        "bridge_check_access": {},
    }
    for name, arguments in calls.items():
        result = _unwrap(asyncio.run(server.call_tool(name, arguments)))
        assert result == expected[name]

    assert seen == calls


# Missing outbound Jira credentials leave the MCP server available, unlike missing
# bearer-token auth. Each bridge call returns a typed error naming the absent input.
# It must neither crash nor report a false PASS.


def _access_check():

    return importlib.import_module("rebar.reconciler.access_check")


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({}, "missing_credentials"),
        ({"JIRA_URL": "https://example.atlassian.net"}, "missing_credentials"),
        (
            {
                "JIRA_URL": "https://example.atlassian.net",
                "JIRA_USER": "someone@example.com",
            },
            "missing_credentials",
        ),
        # JIRA_PROJECT is required even with all credentials. Scope resolution reads the
        # environment, not rebar.toml's [jira] project.
        (
            {
                "JIRA_URL": "https://example.atlassian.net",
                "JIRA_USER": "someone@example.com",
                "JIRA_API_TOKEN": "unused-placeholder-not-a-real-token",
            },
            "missing_project",
        ),
    ],
)
def test_incomplete_jira_scope_yields_a_typed_verdict_not_a_crash(
    env: dict[str, str], reason: str
) -> None:
    """Incomplete scope returns a named exit-2 error before constructing a network client."""
    result, lines, returncode = _access_check().run_access_check(env=env)

    assert returncode == 2
    assert result["verdict"] == "INVALID"
    assert result["reason"] == reason
    assert lines == [f"PROBE_FAIL reason={reason}"]


def test_absent_credentials_surface_as_a_typed_error_naming_the_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library names missing Jira inputs in a RebarError while other tools stay usable.

    Clear JIRA_* explicitly so a configured machine cannot make an outbound Jira call.
    """
    for variable in ("JIRA_URL", "JIRA_USER", "JIRA_PROJECT", "JIRA_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.bridge_check_access()

    assert excinfo.value.returncode == 2
    message = str(excinfo.value)
    for variable in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        assert variable in message, f"the error must name {variable} so an operator can act"
