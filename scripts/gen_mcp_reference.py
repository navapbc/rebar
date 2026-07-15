#!/usr/bin/env python3
"""Generate ``docs/mcp-reference.md`` — the canonical reference of the MCP tools the
rebar MCP server exposes, grouped by gate tier (ticket 235a).

The reference is DERIVED from the server's OWN registrars (``register_read_tools`` /
``register_llm_tools`` / ``register_write_tools``), so a CI drift gate can regenerate it
and fail the build on any diff — a newly-registered tool cannot ship undocumented.

Each registrar is enumerated onto its OWN ``FastMCP`` instance so the registrar is the
ground-truth grouping. Enumeration runs with ``REBAR_MCP_READONLY`` OFF (the write
registrar returns early when read-only, so its tools would otherwise be absent) and the
LLM/Jira gates ON — the gate helpers only fire at *call* time, so registration itself is
unaffected, but flipping them keeps the enumeration environment unambiguous.

Classification: three gate-tier sections — Read-only (always) / LLM-gated
(``REBAR_MCP_ALLOW_LLM``) / Write-gated (``REBAR_MCP_READONLY``). The default gate is the
registrar, with a closed set of hybrid special-cases annotated inline (``reconcile`` /
``fsck`` in the read section, ``run_workflow`` in the write section, and ``sign_review``
which lives in the LLM registrar but is write-gated and is therefore placed in the
Write-gated section).

Usage:
    python scripts/gen_mcp_reference.py           # regenerate docs/mcp-reference.md
    python scripts/gen_mcp_reference.py --check    # exit non-zero if the committed file is stale
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "mcp-reference.md"

# ``sign_review`` is registered by the LLM registrar but writes a SIGNATURE event and is
# gated by ``REBAR_MCP_READONLY`` (not the LLM gate), so it is documented under the
# Write-gated section. This is the ONLY tool whose section differs from its registrar.
_LLM_REGISTRAR_BUT_WRITE_GATED = "sign_review"

# Inline annotations for the closed set of hybrid special-cases (verified against the
# registrar source: _mcp_reads.reconcile/fsck, _mcp_llm.sign_review, _mcp_writes.run_workflow).
_ANNOTATIONS: dict[str, str] = {
    "reconcile": (
        "live/mutating modes are blocked by `REBAR_MCP_READONLY` first, then require "
        "`REBAR_MCP_ALLOW_JIRA_SYNC`; dry-run/check are always available"
    ),
    "fsck": "the recover path is gated by `REBAR_MCP_READONLY` (plain fsck is read-only)",
    _LLM_REGISTRAR_BUT_WRITE_GATED: (
        "hybrid: in the LLM registrar, but write-gated (`REBAR_MCP_READONLY`) — it "
        "persists a signature event, not a billable LLM call"
    ),
    "run_workflow": (
        "live workflows whose steps make LLM calls additionally require `REBAR_MCP_ALLOW_LLM`"
    ),
}


def _registrar_tools() -> dict[str, dict[str, str]]:
    """Enumerate each registrar onto its own FastMCP and return
    ``{"read"|"llm"|"write": {tool_name: one_line_summary}}``.

    Env is set BEFORE importing the server so the ctx gate helpers resolve with write
    tools present and the LLM/Jira gates on."""
    os.environ["REBAR_MCP_READONLY"] = "0"
    os.environ["REBAR_MCP_ALLOW_LLM"] = "1"
    os.environ["REBAR_MCP_ALLOW_JIRA_SYNC"] = "1"

    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP

    import rebar.mcp_server as ms
    from rebar._mcp_llm import register_llm_tools
    from rebar._mcp_reads import register_read_tools
    from rebar._mcp_writes import register_write_tools

    def _group(reg) -> dict[str, str]:
        m = FastMCP("x")
        ctx = SimpleNamespace(
            readonly=ms._readonly,
            allow_llm=ms._allow_llm,
            allow_jira_sync=ms._allow_jira_sync,
            cap_workflow_payload=ms._cap_workflow_payload,
            dump=ms._dump,
            MODE_CAPS=ms.MODE_CAPS,
            Mode=ms.Mode,
            logger=ms.logger,
        )
        reg(m, ctx)
        tools = m._tool_manager._tools  # private handle: the registered-tool map
        out: dict[str, str] = {}
        for name in sorted(tools):
            desc = tools[name].description or ""
            first = desc.splitlines()[0].strip() if desc else ""
            out[name] = first
        return out

    return {
        "read": _group(register_read_tools),
        "llm": _group(register_llm_tools),
        "write": _group(register_write_tools),
    }


def enumerate_by_registrar() -> dict[str, list[str]]:
    """Return the RAW registrar grouping ``{"read"|"llm"|"write": [sorted names]}``.

    This is the ground-truth grouping BY REGISTRAR — the section placement in
    ``render()`` differs only for ``sign_review`` (llm registrar -> Write-gated section)."""
    tools = _registrar_tools()
    return {key: sorted(tools[key]) for key in ("read", "llm", "write")}


def _gate_env_descriptions() -> dict[str, str]:
    """Pull the three gate env-var descriptions from the server's canonical contract."""
    from rebar.mcp_server import MCP_ENV_VARS

    return {v["name"]: v["description"] for v in MCP_ENV_VARS}


def _render_row(name: str, summary: str) -> str:
    annotation = _ANNOTATIONS.get(name)
    cell = summary
    if annotation:
        cell = f"{summary} _({annotation})_" if summary else f"_{annotation}_"
    return f"| `{name}` | {cell} |"


def render() -> str:
    tools = _registrar_tools()
    env = _gate_env_descriptions()

    read = dict(tools["read"])
    # sign_review moves from the LLM registrar into the Write-gated section.
    llm = {n: s for n, s in tools["llm"].items() if n != _LLM_REGISTRAR_BUT_WRITE_GATED}
    write = dict(tools["write"])
    if _LLM_REGISTRAR_BUT_WRITE_GATED in tools["llm"]:
        write[_LLM_REGISTRAR_BUT_WRITE_GATED] = tools["llm"][_LLM_REGISTRAR_BUT_WRITE_GATED]

    total = len(read) + len(llm) + len(write)

    lines: list[str] = []
    lines.append("# MCP tool reference")
    lines.append("")
    lines.append(
        "**Generated by `scripts/gen_mcp_reference.py` — do not edit by hand.** Run "
        "`python scripts/gen_mcp_reference.py` to regenerate; a CI drift gate fails the "
        "build if this file is stale."
    )
    lines.append("")
    lines.append(
        "The tools the rebar MCP server (`rebar-mcp`) exposes, enumerated from the "
        "server's own registrars and grouped by gate tier. Each tool is listed with the "
        "first line of its description; the closed set of hybrid gate cases carry an "
        "inline note."
    )
    lines.append("")

    lines.append("## Read-only (always available)")
    lines.append("")
    lines.append(
        "Registered by `register_read_tools` and always exposed — reads never mutate the "
        "store. Two rows carry an inline gate note (their write/mutation path is gated)."
    )
    lines.append("")
    lines.append("| Tool | Summary |")
    lines.append("|------|---------|")
    for name in sorted(read):
        lines.append(_render_row(name, read[name]))
    lines.append("")

    lines.append("## LLM-gated (`REBAR_MCP_ALLOW_LLM`)")
    lines.append("")
    lines.append(
        f"Registered by `register_llm_tools` and always present, but each makes a live, "
        f"billable LLM call and is disabled at call time unless `REBAR_MCP_ALLOW_LLM` is "
        f"set: {env.get('REBAR_MCP_ALLOW_LLM', '')}"
    )
    lines.append("")
    lines.append("| Tool | Summary |")
    lines.append("|------|---------|")
    for name in sorted(llm):
        lines.append(_render_row(name, llm[name]))
    lines.append("")

    lines.append("## Write-gated (`REBAR_MCP_READONLY`)")
    lines.append("")
    lines.append(
        f"Registered by `register_write_tools`, which is skipped entirely when the "
        f"server is read-only — so these mutation tools are ABSENT under "
        f"`REBAR_MCP_READONLY`: {env.get('REBAR_MCP_READONLY', '')} "
        f"(`sign_review` is registered by the LLM registrar but is write-gated, so it is "
        f"listed here.)"
    )
    lines.append("")
    lines.append("| Tool | Summary |")
    lines.append("|------|---------|")
    for name in sorted(write):
        lines.append(_render_row(name, write[name]))
    lines.append("")

    lines.append("## Gate environment variables")
    lines.append("")
    lines.append("| Variable | Meaning |")
    lines.append("|----------|---------|")
    # Iterate MCP_ENV_VARS so every honored REBAR_MCP_* gate gets a row (can't drift
    # from the manifest). REBAR_ROOT is a path input, not a gate — skip it here.
    for var, desc in env.items():
        if not var.startswith("REBAR_MCP_"):
            continue
        lines.append(f"| `{var}` | {desc} |")
    lines.append("")
    lines.append(f"_{total} tools._")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the MCP tool reference.")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if the committed file is stale"
    )
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != generated:
            sys.stderr.write(
                "docs/mcp-reference.md is stale — regenerate with "
                "`python scripts/gen_mcp_reference.py`\n"
            )
            return 1
        return 0
    DOC_PATH.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
