"""Happy-path oracle for 8a31 — MCP failures carry the CLI's structured error identity.

This file is the ONLY oracle the implementer sees. It pins the core contract:

  * ``rebar.error_code_for(exc)`` classifies rebar exceptions to a stable machine code.
  * ``rebar.KNOWN_ERROR_CODES`` is the single shared vocabulary.
  * an MCP tool driven to a resolvable failure raises ``ToolError`` whose ``__cause__`` is an
    ``McpEnvelopeError`` carrying a structured ``error_envelope`` dict.

Edge / cross-interface / regression cases are held out (validated by the orchestrator).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env


def _clean_env(root: Path) -> dict:

    env = subprocess_env(REBAR_ROOT=str(root))
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        env.pop(var, None)
    return env


def _fresh_tracker(tmp: Path) -> Path:
    env = _clean_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
    subprocess.run(["rebar", "init"], cwd=tmp, check=True, capture_output=True, env=env)
    return tmp


def _call_tool(tool: str, args: dict, root: Path):
    import os

    os.environ["REBAR_ROOT"] = str(root)
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        os.environ.pop(var, None)
    from rebar.mcp_server import build_server

    return asyncio.run(build_server().call_tool(tool, args))


# ---------------------------------------------------------------------------
# H1 — the central classifier maps the core exception identities.
# ---------------------------------------------------------------------------
def test_error_code_for_classifies_core_identities() -> None:
    import rebar
    from rebar._engine_support.reads import TicketNotFoundError
    from rebar._errors import ConcurrencyError, RebarError

    assert rebar.error_code_for(TicketNotFoundError("Ticket 'x' not found")) == "ticket_not_found"
    assert rebar.error_code_for(ConcurrencyError("already claimed")) == "concurrency_conflict"

    # a generic RebarError carrying an explicit error_code is honored
    tagged = RebarError("boom")
    tagged.error_code = "invalid_ticket_type"
    assert rebar.error_code_for(tagged) == "invalid_ticket_type"

    # an unclassifiable failure falls back to a defined residual code
    assert rebar.error_code_for(RebarError("mystery")) == "command_failed"


# ---------------------------------------------------------------------------
# H2 — the shared vocabulary exists and contains the core codes.
# ---------------------------------------------------------------------------
def test_known_error_codes_is_the_shared_vocabulary() -> None:
    import rebar

    assert isinstance(rebar.KNOWN_ERROR_CODES, frozenset)
    for code in (
        "ticket_not_found",
        "concurrency_conflict",
        "claim_failed",
        "command_failed",
    ):
        assert code in rebar.KNOWN_ERROR_CODES


# ---------------------------------------------------------------------------
# H3 — an MCP read tool driven to a missing ticket delivers a structured envelope
#      on the raised ToolError's __cause__.
# ---------------------------------------------------------------------------
def test_mcp_missing_ticket_delivers_structured_envelope(tmp_path: Path) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from rebar._mcp_errors import McpEnvelopeError

    root = _fresh_tracker(tmp_path)
    with pytest.raises(ToolError) as ei:
        _call_tool("show_ticket", {"ticket_id": "abcd-1234-5678-9abc"}, root)

    cause = ei.value.__cause__
    assert isinstance(cause, McpEnvelopeError)
    env = cause.envelope
    assert env["error"] == "ticket_not_found"
    assert env["message"]  # human text preserved, non-empty
