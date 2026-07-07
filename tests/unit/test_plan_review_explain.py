"""WS10 (epic cite-stone-sea / glad-gloom-bog): `rebar explain`, the registry-derived criteria
guide + parity, and coach deep-links.

`explain_criterion` is the ONE shared lookup behind the CLI, the MCP read tool, and the library;
the three error states (unknown-id / malformed-registry / missing-file) are asserted across the
CLI and MCP surfaces. The guide parity check fails when a criterion's section is removed, and the
Pass-4 coaching notes carry an additive `guide_url` deep-link anchored to `#<criterion-id>`.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import registry

pytestmark = pytest.mark.unit


class _FakeMcp:
    """A minimal FastMCP stand-in: its `.tool(...)` decorator just captures the function."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, **_kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _mcp_explain_tool():
    import types

    from rebar import _mcp_reads

    m = _FakeMcp()
    ctx = types.SimpleNamespace(
        readonly=False,
        allow_jira_sync=False,
        cap_workflow_payload=lambda *a, **k: None,
        MODE_CAPS={},
        Mode=None,
    )
    _mcp_reads.register_read_tools(m, ctx=ctx)
    return m.tools["explain_criterion"]


# ── library: the shared lookup + its three error states ─────────────────────────
def test_explain_criterion_success() -> None:
    section = registry.explain_criterion("F1")
    assert section.startswith("## F1")


def test_explain_criterion_unknown_id() -> None:
    with pytest.raises(registry.ExplainError) as ei:
        registry.explain_criterion("BOGUS")
    assert ei.value.kind == "unknown-id"


def test_explain_criterion_missing_file(tmp_path) -> None:
    # a repo root with no generated guide -> missing-file
    with pytest.raises(registry.ExplainError) as ei:
        registry.explain_criterion("F1", repo_root_path=str(tmp_path))
    assert ei.value.kind == "missing-file"


def test_explain_criterion_malformed_registry(monkeypatch) -> None:
    def _boom(**_kw):
        raise ValueError("routing json is corrupt")

    monkeypatch.setattr(registry, "load_criteria", _boom)
    with pytest.raises(registry.ExplainError) as ei:
        registry.explain_criterion("F1")
    assert ei.value.kind == "malformed-registry"


# ── CLI surface ─────────────────────────────────────────────────────────────────
def test_explain_cli_success_and_unknown(capsys) -> None:
    from rebar._cli import main

    assert main(["explain", "F1"]) == 0
    assert "## F1" in capsys.readouterr().out
    assert main(["explain", "BOGUS"]) == 1  # unknown-id -> non-zero + clear message on stderr
    assert "unknown criterion" in capsys.readouterr().err


def test_explain_cli_missing_and_malformed(monkeypatch, tmp_path) -> None:
    from rebar._cli import main

    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))  # no guide under this root -> missing-file
    assert main(["explain", "F1"]) == 1

    def _boom(**_kw):
        raise ValueError("corrupt")

    monkeypatch.setattr(registry, "load_criteria", _boom)  # malformed-registry -> non-zero
    assert main(["explain", "F1"]) == 1


# ── MCP surface (a pure read tool; NOT gated on REBAR_MCP_ALLOW_LLM) ─────────────
def test_explain_mcp_success_and_error_states(monkeypatch, tmp_path) -> None:
    tool = _mcp_explain_tool()
    ok = tool("F1")
    assert ok["criterion_id"] == "F1" and ok["section"].startswith("## F1")

    unknown = tool("BOGUS")
    assert unknown["kind"] == "unknown-id" and "error" in unknown

    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    assert _mcp_explain_tool()("F1")["kind"] == "missing-file"

    def _boom(**_kw):
        raise ValueError("corrupt")

    monkeypatch.setattr(registry, "load_criteria", _boom)
    assert _mcp_explain_tool()("F1")["kind"] == "malformed-registry"


# ── guide parity ────────────────────────────────────────────────────────────────
def test_criteria_guide_parity_fails_on_removed_section(tmp_path) -> None:
    guide = tmp_path / "docs" / "plan-review-criteria-guide.md"
    guide.parent.mkdir(parents=True)
    # a guide with only ONE criterion section -> every OTHER CANONICAL_LLM criterion is a problem
    guide.write_text("# guide\n\n## F1\nbody\n", encoding="utf-8")
    problems = registry.validate_criteria_guide(repo_root_path=str(tmp_path))
    assert problems
    assert any("has no `## G3` section" in p for p in problems)


def test_criteria_guide_in_sync_with_registry() -> None:
    # the committed generated guide covers every CANONICAL_LLM criterion (regenerate-in-place gate)
    assert registry.validate_criteria_guide() == []


# ── coach deep-links ──────────────────────────────────────────────────────────────
def test_coach_deeplink_emitted_and_parseable() -> None:
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    ctx = PlanContext(ticket_id="T-10", ticket_type="task", title="t", description="d")
    parts = {
        "blocking": [],
        "surfaced": [{"id": "f1", "criteria": ["F1"], "finding": "x"}],
        "overflow": [],
        "indeterminate": [],
        "dropped": [],
    }
    coaching = [
        {"move_id": "9", "coaching": "Plan how X will be verified.", "finding_refs": ["f1"]}
    ]
    verdict = orchestrator.finalize_verdict(
        ctx, parts, coaching=coaching, coverage={}, runner_name=None, model=None
    )
    # a URL#anchor is emitted, anchored to the finding's criterion (lower-cased)
    note = verdict["coaching"][0]
    assert note["guide_url"].endswith("#f1")
    # a downstream consumer parses coaching[] — the additive field does not break prose reads
    assert note["coaching"] and note["guide_url"].split("#")[-1] == "f1"
