"""MCP relation-scoped unlink parity over the real registered tool."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

import rebar
from rebar.mcp_server import build_server

pytestmark = pytest.mark.interface


def _relations(source: str, target: str, repo: str) -> set[str]:
    deps = rebar.show_ticket(source, repo_root=repo).get("deps") or []
    return {d["relation"] for d in deps if d.get("target_id") == target}


def _double_related_pair(repo: str, suffix: str) -> tuple[str, str]:
    source = str(rebar.create_ticket("task", f"MCP unlink source {suffix}", repo_root=repo))
    target = str(rebar.create_ticket("task", f"MCP unlink target {suffix}", repo_root=repo))
    rebar.link(source, target, "blocks", repo_root=repo)
    rebar.link(source, target, "relates_to", repo_root=repo)
    return source, target


def test_mcp_unlink_exposes_and_honors_optional_relation(rebar_repo) -> None:
    """An explicit selector removes exactly that edge through real FastMCP."""
    repo = str(rebar_repo)
    source, target = _double_related_pair(repo, "explicit")
    server = build_server()
    tool = next(
        candidate
        for candidate in asyncio.run(server.list_tools())
        if candidate.name == "unlink_tickets"
    )

    schema = tool.inputSchema or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    assert "relation" in properties, "live MCP schema dropped the relation selector"
    assert "relation" not in required, "relation must preserve the optional library default"
    relation_schema = properties["relation"]
    assert relation_schema.get("anyOf") == [
        {"type": "string"},
        {"type": "null"},
    ]
    assert _relations(source, target, repo) == {"blocks", "relates_to"}

    asyncio.run(
        server.call_tool(
            "unlink_tickets",
            {"id1": source, "id2": target, "relation": "blocks"},
        )
    )

    assert _relations(source, target, repo) == {"relates_to"}
    assert _relations(target, source, repo) == {"relates_to"}


def test_mcp_unlink_without_relation_preserves_pair_scoped_fallback(rebar_repo) -> None:
    """Omitting the selector still removes the newest relation on the pair."""
    repo = str(rebar_repo)
    source, target = _double_related_pair(repo, "omitted")
    assert _relations(source, target, repo) == {"blocks", "relates_to"}

    asyncio.run(
        build_server().call_tool(
            "unlink_tickets",
            {"id1": source, "id2": target},
        )
    )

    assert _relations(source, target, repo) == {"blocks"}
    assert _relations(target, source, repo) == set()
