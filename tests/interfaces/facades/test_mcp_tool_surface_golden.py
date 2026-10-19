"""The live MCP tool surface equals the committed golden (story masonic-excusable-guppy).

``mcp_tool_surface_golden.json`` pins each tool's ``inputSchema``, ``description``, and
``annotations`` byte-for-byte. It was captured from the schema story's landed tree
(origin/main b1228deb72f9373e748b6b13752f7cfa23a69f13), BEFORE the 48 library-backed tools
were re-implemented as derived tools, with::

    env -u REBAR_MCP_READONLY python -c 'import asyncio, json; \\
      from rebar.mcp_server import build_server; \\
      t = asyncio.run(build_server().list_tools()); \\
      print(json.dumps({x.name: {"inputSchema": x.inputSchema, \\
        "description": x.description, \\
        "annotations": x.annotations.model_dump() if x.annotations else None} \\
        for x in t}, indent=1, sort_keys=True))'

``REBAR_MCP_ALLOW_LLM`` does not change the advertised surface (it is a call-time gate),
but ``REBAR_MCP_READONLY=1`` registers only 37 of the 64 tools, so the test clears it
before building the server. ``outputSchema`` is deliberately not pinned here: the schema
story's canonical diff test owns it (one owner per contract).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import rebar

pytest.importorskip("mcp")

_GOLDEN = json.loads((Path(__file__).parent / "mcp_tool_surface_golden.json").read_text())


@pytest.fixture(scope="module")
def live_surface() -> dict:
    from rebar.mcp_server import build_server

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("REBAR_MCP_READONLY", raising=False)
        tools = asyncio.run(build_server().list_tools())
    return {
        t.name: {
            "inputSchema": t.inputSchema,
            "description": t.description,
            "annotations": t.annotations.model_dump() if t.annotations else None,
        }
        for t in tools
    }


def test_golden_covers_exactly_the_live_tools(live_surface: dict) -> None:
    assert sorted(live_surface) == sorted(_GOLDEN)


@pytest.mark.parametrize("name", sorted(_GOLDEN))
def test_live_surface_equals_golden(name: str, live_surface: dict) -> None:
    assert live_surface[name] == _GOLDEN[name]
    # ``required`` order is part of the published schema, so compare it as a list too.
    assert live_surface[name]["inputSchema"].get("required") == _GOLDEN[name]["inputSchema"].get(
        "required"
    )


def test_every_derived_tool_names_a_public_library_symbol() -> None:
    from rebar._mcp_tool_specs import TOOL_SPECS

    missing = sorted(spec.name for spec in TOOL_SPECS if spec.library not in rebar.__all__)
    assert missing == []


def test_stripped_params_are_only_repo_root_private_or_declared() -> None:
    from rebar._mcp_tool_specs import TOOL_SPECS

    offenders = sorted(
        f"{spec.name}.{param}"
        for spec in TOOL_SPECS
        for param in spec.strip
        if param != "repo_root" and not param.startswith("_") and param not in spec.divergence
    )
    assert offenders == []


def test_derived_tools_cover_the_library_backed_surface() -> None:
    from rebar._mcp_tool_specs import TOOL_SPECS

    names = [spec.name for spec in TOOL_SPECS]
    assert len(names) == len(set(names)) == 48
    assert set(names) <= set(_GOLDEN)
