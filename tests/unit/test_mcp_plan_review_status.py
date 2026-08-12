"""The MCP server must expose the plan-review attestation currency check (ticket 86c8).

`rebar review-plan <id> --status` is a read-only, no-LLM, no-network query answering
"is my attestation current — should I re-gate before I implement?". It was reachable
from the library and the CLI but had NO MCP tool, so an MCP-only agent could only
discover staleness by provoking a `claim` / `transition ... closed` refusal.

These tests pin the tool onto the READ registrar (so it survives
`REBAR_MCP_READONLY`), pin the two ends of its verdict range, and pin that it
DELEGATES to `rebar.llm.plan_review_status` rather than reimplementing the check —
a second implementation is exactly how the MCP and CLI answers would drift apart.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import pytest

from rebar import _mcp_reads, _mcp_writes

pytestmark = pytest.mark.unit


class _FakeMCP:
    """Collects the decorated tool callables, keyed by tool name."""

    def __init__(self, tools: dict[str, Any], annotations: dict[str, Any]) -> None:
        self._tools = tools
        self._annotations = annotations

    def tool(self, *_a, **kwargs):
        def _decorate(fn):
            self._tools[fn.__name__] = fn
            self._annotations[fn.__name__] = kwargs.get("annotations")
            return fn

        return _decorate


class _FakeCtx:
    """The minimum surface the read registrar reads off ctx."""

    logger = logging.getLogger("test")
    MODE_CAPS: ClassVar[dict[str, Any]] = {}
    Mode = None

    @staticmethod
    def readonly() -> bool:
        return False

    @staticmethod
    def allow_jira_sync() -> bool:
        return False

    @staticmethod
    def allow_llm() -> bool:
        return False

    @staticmethod
    def cap_workflow_payload(obj):
        return obj

    @staticmethod
    def dump(obj):
        return obj


def _read_tools() -> tuple[dict[str, Any], dict[str, Any]]:
    tools: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    _mcp_reads.register_read_tools(_FakeMCP(tools, annotations), ctx=_FakeCtx())
    return tools, annotations


def _write_tools() -> dict[str, Any]:
    tools: dict[str, Any] = {}
    _mcp_writes.register_write_tools(_FakeMCP(tools, {}), ctx=_FakeCtx())
    return tools


def _stub_status(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[str]:
    """Patch the library function the tool must delegate to; record its arguments."""
    import rebar.llm

    seen: list[str] = []

    def _fake(ticket_id: str, **_kwargs) -> dict[str, Any]:
        seen.append(ticket_id)
        return payload

    monkeypatch.setattr(rebar.llm, "plan_review_status", _fake)
    return seen


def test_plan_review_status_is_in_the_read_tool_inventory() -> None:
    """AC1a — registered by the READ registrar, so it survives REBAR_MCP_READONLY."""
    tools, annotations = _read_tools()

    assert "plan_review_status" in tools
    assert annotations["plan_review_status"].readOnlyHint is True
    assert annotations["plan_review_status"].openWorldHint is False


def test_plan_review_status_is_absent_from_the_write_tool_inventory() -> None:
    """AC1b — it is not a mutation, so the write registrar must not claim it."""
    assert "plan_review_status" not in _write_tools()


def test_current_attestation_reports_certified(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2 — a ticket whose attestation is current answers ok/certified."""
    seen = _stub_status(
        monkeypatch,
        {
            "ok": True,
            "verdict": "certified",
            "reason": "certified plan-review attestation",
            "verified_at_sha": "f7e684c3004c987ad5688ea28b815bd4babef08d",
            "signed_at": 1785949495508791001,
        },
    )
    tools, _ = _read_tools()

    result = tools["plan_review_status"]("86c8-4b91-5257-4cca").model_dump()

    assert seen == ["86c8-4b91-5257-4cca"]
    assert result["ok"] is True
    assert result["verdict"] == "certified"
    assert result["verified_at_sha"] == "f7e684c3004c987ad5688ea28b815bd4babef08d"
    assert result["signed_at"] == 1785949495508791001


def test_missing_attestation_reports_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 — no attestation answers not-ok/unsigned, and the anchors stay null."""
    _stub_status(
        monkeypatch,
        {
            "ok": False,
            "verdict": "unsigned",
            "reason": "no plan-review attestation: run `rebar review-plan <id>`",
            "verified_at_sha": None,
            "signed_at": None,
        },
    )
    tools, _ = _read_tools()

    result = tools["plan_review_status"]("86c8-4b91-5257-4cca").model_dump()

    assert result["ok"] is False
    assert result["verdict"] == "unsigned"
    assert result["reason"].startswith("no plan-review attestation")
    assert result["verified_at_sha"] is None
    assert result["signed_at"] is None


def test_tool_delegates_to_the_library_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 — patching rebar.llm.plan_review_status changes the tool's answer, which is
    only true if the tool calls it instead of recomputing the gate itself."""
    _stub_status(
        monkeypatch,
        {
            "ok": False,
            "verdict": "stale-code",
            "reason": "reviewed code changed: src/rebar/_mcp_reads.py",
            "verified_at_sha": "0" * 40,
            "signed_at": 42,
        },
    )
    tools, _ = _read_tools()

    result = tools["plan_review_status"]("86c8-4b91-5257-4cca").model_dump()

    assert result["verdict"] == "stale-code"
    assert result["reason"] == "reviewed code changed: src/rebar/_mcp_reads.py"
    assert result["verified_at_sha"] == "0" * 40
