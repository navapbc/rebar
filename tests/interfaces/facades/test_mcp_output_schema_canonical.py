"""MCP outputSchemas ARE the canonical JSON Schemas (story quilted-nepotic-kitty).

The pydantic output mirror (``rebar._mcp_models``) is gone: every schema-backed tool
advertises its ``src/rebar/schemas/*.json`` file (inlined, self-contained) and returns the
library payload unchanged, so a key the library omits stays ABSENT on the wire rather than
being re-added as ``null``. These tests pin both halves: the advertised bytes, and the real
wire result through an in-memory client session (the SDK validates every structured result
against the advertised schema, so a violating payload would come back as an error).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas

pytest.importorskip("mcp")

from rebar._mcp_schema import OUTPUT_SCHEMAS_BY_TOOL, inline_schema

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _tools(monkeypatch: pytest.MonkeyPatch) -> dict:
    from rebar.mcp_server import build_server

    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    return {t.name: t for t in asyncio.run(build_server().list_tools())}


def _call(name: str, arguments: dict):
    """One real tools/call over an in-memory client session (what a client receives)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from rebar.mcp_server import build_server

    async def drive():
        server = build_server()
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(drive())


def test_every_advertised_output_schema_equals_its_canonical_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _tools(monkeypatch)
    for name, (names, kind) in OUTPUT_SCHEMAS_BY_TOOL.items():
        assert name in tools, f"{name} is in OUTPUT_SCHEMAS_BY_TOOL but not registered"
        advertised = tools[name].outputSchema
        assert advertised == inline_schema(names, kind), name
        if kind == "object":
            canonical = schemas.load(names[0])
            assert advertised["properties"] == json.loads(
                json.dumps(canonical["properties"]).replace(
                    "common.schema.json#/$defs/", "#/$defs/"
                )
            ), name
            assert advertised.get("required") == canonical.get("required"), name


def test_every_schema_backed_tool_is_in_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """An advertiser outside the table would publish FastMCP's auto-derived schema."""
    tools = _tools(monkeypatch)
    stray = sorted(
        name
        for name, tool in tools.items()
        if tool.outputSchema
        and name not in OUTPUT_SCHEMAS_BY_TOOL
        # A plain-string result: FastMCP wraps it as {"result": string}.
        and name != "render_workflow"
    )
    assert stray == []


def test_output_schemas_are_self_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, tool in _tools(monkeypatch).items():
        rendered = json.dumps(tool.outputSchema or {})
        assert ".json#" not in rendered, f"{name} advertises a cross-file $ref"


def test_show_ticket_wire_payload_validates_with_absent_optionals(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "wire shape", repo_root=str(rebar_repo))
    result = _call("show_ticket", {"ticket_id": tid})

    assert not result.isError, result.content
    payload = result.structuredContent
    schemas.validator(schemas.TICKET_STATE).validate(payload)
    for absent in ("last_reopened_at", "source_id", "cross_session_warning"):
        assert absent not in payload, f"{absent} must be absent, not null"
    assert "assignee" in payload and payload["assignee"] is None


def test_ready_tickets_full_and_lean_rows_validate(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "ready work", repo_root=str(rebar_repo))
    lean = _call("ready_tickets", {})
    full = _call("ready_tickets", {"full": True})

    assert not lean.isError and not full.isError
    lean_rows = lean.structuredContent["result"]
    full_rows = full.structuredContent["result"]
    assert lean_rows and full_rows
    for row in lean_rows:
        schemas.validator(schemas.READY_TICKET_SUMMARY).validate(row)
    for row in full_rows:
        schemas.validator(schemas.TICKET_STATE).validate(row)


def test_write_ack_omits_unset_advisories(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "ack shape", repo_root=str(rebar_repo))
    result = _call("comment_ticket", {"ticket_id": tid, "body": "hello"})

    assert not result.isError, result.content
    ack = result.structuredContent
    schemas.validator(schemas.WRITE_ACK).validate(ack)
    assert ack["result"] == "ok"
    assert "description_warning" not in ack and "cross_session_warning" not in ack
    assert ack["push_status"]["state"] in ("ok", "pending", "unknown")
    if ack["push_status"]["state"] == "ok":
        assert ack["push_status"] == {"state": "ok"}


def test_set_file_impact_accepts_typed_entries(rebar_repo: Path) -> None:
    """The input is the canonical file_impact_entry: a typed object, never an explicit null.

    The store still requires a string ``reason`` on every entry (``_commands/leaf.py``), as
    it did before the mirror was removed; the schema change only stops ``null`` from being
    accepted at the transport and silently dropped."""
    tid = rebar.create_ticket("task", "impact input", repo_root=str(rebar_repo))

    accepted = _call(
        "set_file_impact", {"ticket_id": tid, "impact": [{"path": "a.py", "reason": "r"}]}
    )
    assert not accepted.isError, accepted.content
    assert rebar.get_file_impact(tid, repo_root=str(rebar_repo)) == [
        {"path": "a.py", "reason": "r"}
    ]

    refused = _call(
        "set_file_impact", {"ticket_id": tid, "impact": [{"path": "a.py", "reason": None}]}
    )
    assert refused.isError
    assert "reason" in "".join(getattr(block, "text", "") for block in refused.content)


def test_mcp_help_env_lines_unchanged() -> None:
    """`rebar-mcp --help` lists each env var by name only; no entry was ever a deprecated
    alias, so dropping the always-False flag and its suffix changes no output line."""
    from rebar.mcp_server import MCP_ENV_VARS

    assert all(set(entry) == {"name", "description"} for entry in MCP_ENV_VARS)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv=['rebar-mcp','--help']; "
            "from rebar.mcp_server import main; main()",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
    )
    env_lines = proc.stdout.split("Env:\n", 1)[1].splitlines()
    assert env_lines == [f"       {entry['name']}" for entry in MCP_ENV_VARS]
