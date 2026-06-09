"""The compiled ticket-state output must conform to the canonical JSON Schema
across ALL three interfaces (library, CLI, MCP).

This is the prototype slice of the broader "JSON Schema for every output" effort:
one authoritative schema (src/rebar/schemas/ticket_state.schema.json) used as a
cross-interface drift oracle. If any interface's `show` output diverges from the
documented contract, this fails — catching exactly the kind of undocumented-shape
drift the audit ran into.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adapters import CliAdapter, LibraryAdapter, McpAdapter

from rebar import schemas

jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture(params=["library", "cli", "mcp"])
def adapter(request: pytest.FixtureRequest, rebar_repo: Path):
    if request.param == "library":
        return LibraryAdapter()
    if request.param == "cli":
        return CliAdapter()
    return McpAdapter()


def _schema() -> dict:
    return schemas.load(schemas.TICKET_STATE)


def test_show_output_conforms_to_schema(adapter) -> None:
    """A representative ticket (tags + comments populated) validates. Tags and
    comments are added via the uniform adapter methods so the scenario is
    interface-agnostic (create-kwarg shapes differ across CLI/library/MCP)."""
    tid = adapter.create("task", "Schema conformance ticket")
    adapter.tag(tid, "alpha")
    adapter.comment(tid, "a comment")
    state = adapter.show(tid)
    assert state["tags"] == ["alpha"]
    jsonschema.validate(instance=state, schema=_schema())


def test_minimal_ticket_conforms_to_schema(adapter) -> None:
    """A freshly created ticket with only defaults still satisfies the required
    fields (ticket_id/ticket_type/title/status/priority/tags)."""
    tid = adapter.create("bug", "Minimal ticket")
    jsonschema.validate(instance=adapter.show(tid), schema=_schema())


def test_schema_itself_is_valid() -> None:
    """The schema document is itself a valid JSON Schema (draft 2020-12)."""
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)


def test_mcp_show_ticket_advertises_output_schema(rebar_repo: Path) -> None:
    """FastMCP advertises an outputSchema for show_ticket (so agents get a typed
    contract), and it covers the canonical schema's required fields."""
    import asyncio

    from rebar.mcp_server import build_server

    srv = build_server()
    tools = asyncio.run(srv.list_tools())
    show = next(t for t in tools if t.name == "show_ticket")
    out = show.outputSchema or {}
    assert out, "show_ticket should advertise an outputSchema"
    advertised = set(out.get("properties", {}))
    required = set(_schema()["required"])
    missing = required - advertised
    assert not missing, f"MCP outputSchema is missing canonical required fields: {missing}"
