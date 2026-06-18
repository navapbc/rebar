"""WS-ffc4: the workflow status/result MCP tools are typed read tools (advertise
outputSchema), and large results are capped under the token budget."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")


def _tools():
    from rebar.mcp_server import build_server

    return {t.name: t for t in asyncio.run(build_server().list_tools())}


def test_workflow_read_tools_advertise_output_schema() -> None:
    tools = _tools()
    for name in ("get_workflow_status", "get_workflow_result"):
        assert name in tools
        assert tools[name].outputSchema, f"{name} should advertise a typed outputSchema"


def test_run_workflow_is_a_start_ack_no_schema() -> None:
    # The async START tool stays a plain dict (fire-and-forget ack), not a typed
    # result — the typed surface is the status/result reads.
    tools = _tools()
    assert "run_workflow" in tools
    assert not tools["run_workflow"].outputSchema


def test_cap_keeps_small_payloads_intact() -> None:
    from rebar.mcp_server import _cap_workflow_payload

    payload = {"run_id": "r", "status": "succeeded", "outputs": {"a": {"x": 1}}}
    assert _cap_workflow_payload(payload) == payload


def test_cap_truncates_oversized_payloads() -> None:
    from rebar.mcp_server import _cap_workflow_payload

    big = {"big": "z" * 200_000}
    payload = {
        "run_id": "r",
        "status": "succeeded",
        "terminal_output": big,
        "outputs": {"a": big, "b": big},
    }
    capped = _cap_workflow_payload(payload)
    assert capped["truncated"] is True
    assert "_truncated" in capped["terminal_output"]
    assert all("_truncated" in v for v in capped["outputs"].values())
    # And it now fits the budget.
    import json

    assert len(json.dumps(capped)) < 90_000
