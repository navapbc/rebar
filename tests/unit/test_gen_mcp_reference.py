"""Tests for the MCP tool-reference generator (ticket 235a).

The generator (scripts/gen_mcp_reference.py) emits docs/mcp-reference.md from the MCP
server's own registrars, classified into three gate-tier sections (Read-only / LLM-gated
/ Write-gated) with inline annotations for the closed set of hybrid special cases
(reconcile, fsck, sign_review, run_workflow).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_mcp_reference.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_mcp_reference", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()


def _section(doc: str, label: str) -> str:
    """Return the text of the section whose heading contains ``label`` (up to the next
    top-level ``## `` heading)."""
    lines = doc.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if re.match(r"^##\s", line):
            if capturing:
                break
            capturing = label.lower() in line.lower()
            continue
        if capturing:
            out.append(line)
    return "\n".join(out)


# ─────────────────────────── HAPPY PATH ──────────────────────────────────────


def test_render_lists_representative_tools():
    """Representative tools from each registrar appear backtick-wrapped in the output."""
    doc = gen.render()
    for tool in (
        "create_ticket",
        "show_ticket",
        "review_ticket",
        "sign_review",
        "reconcile",
        "run_workflow",
    ):
        assert f"`{tool}`" in doc, f"tool {tool!r} missing from mcp reference"


def test_render_has_three_gate_tier_sections():
    """The three gate-tier section labels are present."""
    doc = gen.render()
    for label in ("Read-only", "LLM-gated", "Write-gated"):
        assert label in doc, f"section {label!r} missing"


def test_check_mode_clean_against_committed_tree():
    """The committed docs/mcp-reference.md matches the generator (exit 0)."""
    assert gen.main(["--check"]) == 0


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_all_enumerated_tools_appear():
    """Every tool the generator enumerates from the registrars appears in the doc."""
    doc = gen.render()
    tools = gen.enumerate_by_registrar()
    for group in tools.values():
        for tool in group:
            assert f"`{tool}`" in doc, f"enumerated tool {tool!r} not rendered"


def test_write_tools_registrar_populated():
    """The write registrar is enumerated (READONLY off), not empty — the classic trap."""
    tools = gen.enumerate_by_registrar()
    assert "create_ticket" in tools["write"]
    assert "run_workflow" in tools["write"]
    assert len(tools["write"]) >= 15  # ~18; robust to minor surface changes


def test_sign_review_in_write_section_not_llm_section():
    """sign_review (hybrid) is placed in the Write-gated section, NOT the LLM-gated one."""
    doc = gen.render()
    write_sec = _section(doc, "Write-gated")
    llm_sec = _section(doc, "LLM-gated")
    assert "sign_review" in write_sec
    assert "sign_review" not in llm_sec


def test_reconcile_annotated_with_both_gates():
    """reconcile's live-mode annotation names BOTH REBAR_MCP_READONLY and
    REBAR_MCP_ALLOW_JIRA_SYNC (readonly blocks first, then jira-sync required)."""
    doc = gen.render()
    read_sec = _section(doc, "Read-only")
    # find the reconcile row/line
    recon_line = next((ln for ln in read_sec.splitlines() if "reconcile" in ln), "")
    assert "REBAR_MCP_READONLY" in recon_line
    assert "REBAR_MCP_ALLOW_JIRA_SYNC" in recon_line


def test_run_workflow_annotated_with_allow_llm():
    """run_workflow (write section) notes the additional REBAR_MCP_ALLOW_LLM gate for
    live LLM-step workflows."""
    doc = gen.render()
    write_sec = _section(doc, "Write-gated")
    rw_line = next((ln for ln in write_sec.splitlines() if "run_workflow" in ln), "")
    assert "REBAR_MCP_ALLOW_LLM" in rw_line


def test_fsck_annotated_readonly_recover():
    """fsck's recover-path annotation names REBAR_MCP_READONLY."""
    doc = gen.render()
    read_sec = _section(doc, "Read-only")
    fsck_line = next((ln for ln in read_sec.splitlines() if re.search(r"`fsck`", ln)), "")
    assert "REBAR_MCP_READONLY" in fsck_line


def test_check_mode_detects_stale_doc(tmp_path: Path, monkeypatch):
    """main(--check) exits non-zero when the committed doc is stale vs the generator."""
    stale = tmp_path / "mcp-reference.md"
    stale.write_text("# MCP reference\n\nstale\n", encoding="utf-8")
    monkeypatch.setattr(gen, "DOC_PATH", stale, raising=False)
    assert gen.main(["--check"]) != 0
