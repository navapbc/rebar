"""Happy-path contracts for the additive library and MCP bridge surfaces."""

from __future__ import annotations

import asyncio
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


# --------------------------------------------------------------------------- #
# Absent Jira credentials DEGRADE SOFTLY (bug colourless-hasteless-lamb)       #
# --------------------------------------------------------------------------- #
#
# The deployed MCP server had no Jira credentials at all, so every live bridge operation
# failed. Wiring them in (ssm.tf -> fetch-secrets.sh -> the container .env) is deliberately
# a SOFT-degrade path: unlike the static bearer-token store, which is an AUTH boundary and
# fails CLOSED, an absent OUTBOUND integration credential must never take the endpoint down.
#
# These pin the behaviour that makes that posture safe: a missing credential produces a
# TYPED, per-call error that NAMES what is missing, not a crash and not a silent success. If
# the probe ever started raising an untyped exception, or worse returning a PASS-shaped
# verdict without credentials, the soft posture would stop being defensible.


def _access_check():
    from rebar._lib_ops import _engine_module

    return _engine_module("rebar_reconciler.access_check")


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
        # All three CREDENTIALS present but no project: the probe still cannot run. This is the
        # case that makes JIRA_PROJECT a fourth REQUIRED input rather than an optional extra --
        # `resolve_jira_probe_scope` reads the ENVIRONMENT ONLY, so rebar.toml's `[jira]
        # project` never reaches it, and wiring only the credentials would move the failure
        # here instead of reaching a verdict.
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
    """Each incomplete scope fails CLOSED with a NAMED reason and exit 2 -- never a traceback.

    No network is reached: the probe short-circuits before constructing a client, which is
    exactly why an unconfigured box stays serviceable instead of hanging or erroring out.
    """
    result, lines, returncode = _access_check().run_access_check(env=env)

    assert returncode == 2
    assert result["verdict"] == "INVALID"
    assert result["reason"] == reason
    assert lines == [f"PROBE_FAIL reason={reason}"]


def test_absent_credentials_surface_as_a_typed_error_naming_the_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library boundary converts the exit-2 probe into a RebarError that NAMES the inputs.

    This is the whole soft-degrade contract at the tool boundary: ONE tool reports a clear,
    actionable "these are required" error while the server keeps serving every other tool.
    An operator reading it learns which SSM slot to populate without opening the source.

    The JIRA_* variables are cleared explicitly rather than assumed absent. A developer machine
    (and, once this wiring lands, the box itself) HAS them set, so without this the test would
    make a LIVE authenticated call to Jira -- slow, flaky, credential-dependent, and no longer
    a test of the unconfigured path at all.
    """
    for variable in ("JIRA_URL", "JIRA_USER", "JIRA_PROJECT", "JIRA_API_TOKEN"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.bridge_check_access()

    assert excinfo.value.returncode == 2
    message = str(excinfo.value)
    for variable in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        assert variable in message, f"the error must name {variable} so an operator can act"
