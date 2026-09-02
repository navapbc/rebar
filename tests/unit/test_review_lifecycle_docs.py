"""Canonical review/gate lifecycle descriptions must match shipped runtime (ticket
ecb4-ceda-e276-4efb).

The plan-review DET floor is P1-P11 in production (``det_floor.py``,
``plan_review/registry.py``), but the outward-facing ``review_plan`` MCP tool
description (and its generated ``docs/mcp-reference.md`` row) advertised P1-P9. The
shared review kernel and prompting layer also described the code-review gate as
"future" after it shipped (epic ``b744``; see ``rebar.llm.code_review``). These tests
pin the corrected, current state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _review_plan_description() -> str:
    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP

    import rebar.mcp_server as ms
    from rebar._mcp_llm import register_llm_tools

    mcp = FastMCP("review-plan-lifecycle-description-contract")
    ctx = SimpleNamespace(
        readonly=ms._readonly,
        allow_llm=ms._allow_llm,
        dump=ms._dump,
        logger=ms.logger,
    )
    register_llm_tools(mcp, ctx)
    return mcp._tool_manager._tools["review_plan"].description or ""


# ─────────────────────────── HAPPY PATH ──────────────────────────────────────


def test_review_plan_description_names_the_current_p1_p11_floor():
    """The canonical review_plan tool description names the shipped P1-P11 DET floor,
    not the stale pre-registry-expansion P1-P9 count."""
    description = _review_plan_description()
    assert "P1-P11" in description or "P1\u2013P11" in description
    assert "P1-P9" not in description and "P1\u2013P9" not in description


def test_mcp_reference_doc_review_plan_row_names_p1_p11():
    """docs/mcp-reference.md is generated from the corrected description and must
    likewise name P1-P11 for review_plan, not the stale P1-P9."""
    doc = (REPO_ROOT / "docs" / "mcp-reference.md").read_text(encoding="utf-8")
    review_plan_line = next((ln for ln in doc.splitlines() if "`review_plan`" in ln), "")
    assert review_plan_line, "review_plan row missing from docs/mcp-reference.md"
    assert "P1-P11" in review_plan_line or "P1\u2013P11" in review_plan_line
    assert "P1-P9" not in review_plan_line and "P1\u2013P9" not in review_plan_line


def test_shared_kernel_module_docstrings_do_not_call_code_review_future():
    """review_kernel and prompting modules describe the code-review gate as shipped
    (epic b744), not as a future gate -- it has already landed."""
    targets = [
        REPO_ROOT / "src" / "rebar" / "llm" / "review_kernel" / "__init__.py",
        REPO_ROOT / "src" / "rebar" / "llm" / "review_kernel" / "discovery.py",
        REPO_ROOT / "src" / "rebar" / "llm" / "prompting" / "prompts.py",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"future code-review", text, re.IGNORECASE), (
            f"{path} still calls the shipped code-review gate 'future'"
        )


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_det_floor_seam_boundary_comments_keep_p1_p9_verbatim():
    """The STATIC, frozen pre-expansion DET-floor seam comments in det_advisory.py and
    det_lint.py deliberately keep 'P1-P9' -- they document the historical frozen seam
    boundary (the floor was P1-P9 before P10/P11 were added), not the current overall
    floor count, and must NOT be mass-corrected to P1-P11 (see .joe-janitor/tools.md
    'P1-P9 seam boundaries (KEEP, not stale)')."""
    det_advisory = (
        REPO_ROOT / "src" / "rebar" / "llm" / "plan_review" / "det_advisory.py"
    ).read_text(encoding="utf-8")
    det_lint = (REPO_ROOT / "src" / "rebar" / "llm" / "plan_review" / "det_lint.py").read_text(
        encoding="utf-8"
    )
    assert "P1-P9" in det_advisory or "P1\u2013P9" in det_advisory
    assert "P1-P9" in det_lint or "P1\u2013P9" in det_lint


def test_gen_mcp_reference_check_mode_passes_against_committed_doc():
    """The regenerated docs/mcp-reference.md is byte-consistent with the generator
    after the description fix -- proves the doc was actually regenerated, not
    hand-edited out of sync with its source."""
    import importlib.util

    gen_path = REPO_ROOT / "scripts" / "gen_mcp_reference.py"
    spec = importlib.util.spec_from_file_location("gen_mcp_reference", gen_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--check"]) == 0
