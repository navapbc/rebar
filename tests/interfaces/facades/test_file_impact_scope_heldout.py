"""Held-out edge and end-to-end contracts for tri-state file impact."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from adapters import _unwrap

import rebar
from rebar import schemas


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _tickets_ref(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "tickets"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_undeclared_and_paths_none_paths_transitions(rebar_repo: Path) -> None:
    root = str(rebar_repo)
    ticket_id = rebar.create_ticket("task", "Transitions", repo_root=root)
    first = [{"path": "src/first.py", "reason": "implementation"}]
    second = [{"path": "src/second.py", "reason": "implementation"}]

    assert rebar.get_file_impact_scope(ticket_id, repo_root=root) == {
        "kind": "undeclared",
        "reason": "",
        "paths": [],
    }

    rebar.set_file_impact(ticket_id, first, repo_root=root)
    assert rebar.get_file_impact_scope(ticket_id, repo_root=root) == {
        "kind": "paths",
        "reason": "",
        "paths": first,
    }

    rebar.declare_no_file_impact(
        ticket_id,
        "operator action only",
        repo_root=root,
    )
    assert rebar.get_file_impact_scope(ticket_id, repo_root=root) == {
        "kind": "none",
        "reason": "operator action only",
        "paths": [],
    }

    rebar.set_file_impact(ticket_id, second, repo_root=root)
    assert rebar.get_file_impact_scope(ticket_id, repo_root=root) == {
        "kind": "paths",
        "reason": "",
        "paths": second,
    }
    assert rebar.get_file_impact(ticket_id, repo_root=root) == second


@pytest.mark.parametrize(
    ("tail", "message_fragment"),
    [
        (("--none", "123456789"), "10 non-whitespace"),
        (("--none",), "--none"),
        ((), "<json_array>"),
        (
            (
                '[{"path":"src/a.py","reason":"legacy"}]',
                "--none",
                "1234567890",
            ),
            "--none",
        ),
    ],
)
def test_invalid_cli_forms_exit_two_without_writing(
    rebar_repo: Path,
    tail: tuple[str, ...],
    message_fragment: str,
) -> None:
    root = str(rebar_repo)
    ticket_id = rebar.create_ticket("task", "Invalid forms", repo_root=root)
    before_ref = _tickets_ref(rebar_repo)
    before_state = rebar.show_ticket(ticket_id, repo_root=root)

    result = _cli(rebar_repo, "set-file-impact", ticket_id, *tail)

    assert result.returncode == 2
    assert message_fragment in result.stderr
    assert _tickets_ref(rebar_repo) == before_ref
    assert rebar.show_ticket(ticket_id, repo_root=root) == before_state


def test_reason_boundary_and_legacy_array_cli_contract(rebar_repo: Path) -> None:
    root = str(rebar_repo)
    ticket_id = rebar.create_ticket("task", "Boundary", repo_root=root)

    accepted = _cli(
        rebar_repo,
        "set-file-impact",
        ticket_id,
        "--none",
        "1234567890",
    )
    assert accepted.returncode == 0, accepted.stderr

    impact = [{"path": "src/legacy.py", "reason": "legacy form"}]
    legacy = _cli(
        rebar_repo,
        "set-file-impact",
        ticket_id,
        json.dumps(impact),
    )
    assert legacy.returncode == 0, legacy.stderr
    assert legacy.stdout == f"impact set on {ticket_id}: 1 paths\n"

    read_back = _cli(rebar_repo, "get-file-impact", ticket_id)
    assert read_back.returncode == 0, read_back.stderr
    assert json.loads(read_back.stdout) == impact
    assert rebar.get_file_impact_scope(ticket_id, repo_root=root) == {
        "kind": "paths",
        "reason": "",
        "paths": impact,
    }


def test_help_and_show_surface_both_forms_and_state(rebar_repo: Path) -> None:
    root = str(rebar_repo)
    ticket_id = rebar.create_ticket("task", "Visible state", repo_root=root)
    reason = "external coordination only"

    help_result = _cli(rebar_repo, "help", "set-file-impact")
    assert help_result.returncode == 0
    assert "<json_array>" in help_result.stdout
    assert '--none "<reason>"' in help_result.stdout

    declared = _cli(
        rebar_repo,
        "set-file-impact",
        ticket_id,
        "--none",
        reason,
    )
    assert declared.returncode == 0, declared.stderr

    shown = _cli(rebar_repo, "show", ticket_id)
    assert shown.returncode == 0, shown.stderr
    state = json.loads(shown.stdout)
    assert state["file_impact_scope"] == "none"
    assert state["no_file_impact_reason"] == reason


def test_mcp_write_validation_and_schema_contract(
    rebar_repo: Path,
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    from rebar.mcp_server import build_server

    root = str(rebar_repo)
    ticket_id = rebar.create_ticket("task", "MCP declaration", repo_root=root)
    server = build_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert "declare_no_file_impact" in tools
    assert not tools["declare_no_file_impact"].outputSchema

    result = _unwrap(
        asyncio.run(
            server.call_tool(
                "declare_no_file_impact",
                {
                    "ticket_id": ticket_id,
                    "reason": "external system action",
                },
            )
        )
    )
    assert result == "ok"

    state = rebar.show_ticket(ticket_id, repo_root=root)
    assert state["file_impact_scope"] == "none"
    assert state["no_file_impact_reason"] == "external system action"
    schemas.validator(schemas.TICKET_STATE).validate(state)

    before_ref = _tickets_ref(rebar_repo)
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            server.call_tool(
                "declare_no_file_impact",
                {
                    "ticket_id": ticket_id,
                    "reason": "short",
                },
            )
        )
    cause = exc_info.value.__cause__
    # 8a31: MCP failures carry a structured envelope on McpEnvelopeError; the original
    # RebarError (returncode 2) is preserved one level deeper on its __cause__.
    from rebar._mcp_errors import McpEnvelopeError

    assert isinstance(cause, McpEnvelopeError)
    assert cause.envelope["error"] == "command_failed"
    assert cause.envelope["exit_code"] == 2
    engine = cause.__cause__
    assert isinstance(engine, rebar.RebarError)
    assert engine.returncode == 2
    assert _tickets_ref(rebar_repo) == before_ref


def test_ticket_state_schema_rejects_unknown_file_impact_scope(
    rebar_repo: Path,
) -> None:
    ticket_id = rebar.create_ticket(
        "task",
        "Schema scope validation",
        repo_root=str(rebar_repo),
    )
    invalid_state = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    invalid_state["file_impact_scope"] = "unknown"

    with pytest.raises(jsonschema.ValidationError):
        schemas.validator(schemas.TICKET_STATE).validate(invalid_state)
