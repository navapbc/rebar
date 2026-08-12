"""The MCP `link_tickets` tool must report an escalated substitution (ticket fec5).

Blocking links between tickets that do not share a parent are escalated to
comparable endpoints, so the RECORDED edge can differ from the requested one. The
CLI prints a redirect record; the MCP path cannot — `link_core(quiet=True)`
suppresses stdout precisely because rebar-mcp speaks MCP-over-stdio and a stray
print would corrupt the JSON-RPC stream. The tool therefore has to say so in its
return value, or the agent is told "ok" for an edge that was never written.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from rebar import _mcp_writes


def _collect_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Register the write tools against a fake MCP and hand back the callables."""
    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    class _FakeCtx:
        """The minimum register_write_tools reads off ctx (it gates on readonly())."""

        logger = logging.getLogger("test")

        @staticmethod
        def readonly() -> bool:
            return False

        @staticmethod
        def dump(obj):
            return obj

        @staticmethod
        def allow_llm() -> bool:
            return False

    _mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())
    return tools


@pytest.mark.unit
def test_link_tickets_returns_plain_ok_when_nothing_was_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ack text is unchanged, so existing consumers keep working.

    Bug vapoury-attack-lamb widened the return from a bare ``"ok"`` string to the shared
    ``WriteAckOut`` — a strict SUPERSET, since FastMCP already advertised these tools as
    ``{"result": <str>}``. This pins that the widening did not change the TEXT, and that the
    added delivery field is present rather than silently omitted.
    """
    monkeypatch.setattr(_mcp_writes.rebar, "link", lambda *_a, **_k: None)
    tools = _collect_tools(monkeypatch)

    out = tools["link_tickets"]("a", "b", "depends_on")

    assert out.result == "ok"
    assert out.push_status is not None, "the write ack dropped its push-delivery status"


@pytest.mark.unit
def test_link_tickets_names_both_pairs_when_it_escalated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The substitution is stated in full: what was asked for AND what was recorded."""
    record = {
        "redirected": True,
        "original": {"source": "leaf-a", "target": "epic-b"},
        "resolved": {"source": "epic-a", "target": "epic-b"},
    }
    monkeypatch.setattr(_mcp_writes.rebar, "link", lambda *_a, **_k: record)
    tools = _collect_tools(monkeypatch)

    out = tools["link_tickets"]("leaf-a", "epic-b", "depends_on")

    assert out.result == "ok (escalated: leaf-a->epic-b recorded as epic-a->epic-b)", out
