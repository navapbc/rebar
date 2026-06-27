"""Safeguard + run_workflow coverage for the repo-snapshot process (epic raze-vet-ditch).

Two gaps surfaced after the epic merged:
- (e95a) generic run_workflow agent steps read the mutable checkout, with no ref/source and
  outside the LLM gate.
- (d6cc) nothing prevented a NEW tool-using agent op from bypassing the snapshot process.

These tests pin the fix: a runtime fail-closed guard (config.assert_gated) enforced at the
runner's agentic-tool wiring, run_workflow gating LLM workflows through the snapshot, and the
MCP run_workflow tool fenced behind REBAR_MCP_ALLOW_LLM.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm  # noqa: F401
from rebar.llm import config as llmcfg
from rebar.llm import gate_source
from rebar.llm.workflow import runs


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


# --------------------------------------------------------------------------------------
# d6cc — the fail-closed guard
# --------------------------------------------------------------------------------------
def test_assert_gated_raises_outside_a_gate_session(monkeypatch):
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    assert llmcfg.in_gate_session() is False
    with pytest.raises(RuntimeError) as exc:
        llmcfg.assert_gated("agentic filesystem tools")
    assert "outside the repo-snapshot gate" in str(exc.value).lower()


def test_assert_gated_passes_inside_gate_session():
    with llmcfg.gate_session():
        assert llmcfg.in_gate_session() is True
        llmcfg.assert_gated()  # no raise


def test_gate_read_root_marks_session_for_both_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "g"))
    from rebar._snapshot import SnapshotHandle

    # local handle (no snapshot) still marks a gate session → guard passes.
    local = SnapshotHandle(path=tmp_path, sha=None, source="local")
    with gate_source.gate_read_root(local):
        assert llmcfg.in_gate_session() is True
        llmcfg.assert_gated()
    assert llmcfg.in_gate_session() is False  # reverts


def test_assert_gated_env_override(monkeypatch):
    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
    llmcfg.assert_gated()  # no raise (audited escape hatch)


# --------------------------------------------------------------------------------------
# d6cc — enforced at the runner: a real agent (no model_override) fails closed outside a gate
# --------------------------------------------------------------------------------------
def test_runner_agentic_fails_closed_outside_gate(monkeypatch):
    monkeypatch.delenv("REBAR_GATE_ALLOW_UNGATED", raising=False)
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    cfg = LLMConfig.from_env()
    runner = PydanticAIRunner(cfg, model_override=None)  # real model path (not the test seam)
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["r"])
    # The guard fires when the agentic tools are wired — BEFORE any model/network call.
    with pytest.raises(RuntimeError) as exc:
        runner.run(req)
    assert "outside the repo-snapshot gate" in str(exc.value).lower()


# --------------------------------------------------------------------------------------
# e95a — run_workflow gates LLM workflows through the snapshot; deterministic ones don't
# --------------------------------------------------------------------------------------
_AGENT_DOC = {"name": "wf", "steps": [{"id": "a", "prompt": "ticket-quality"}]}
_DET_DOC = {"name": "wf", "steps": [{"id": "s", "uses": "noop"}]}


def test_has_llm_steps_detects_agent_and_nested(tmp_path):
    assert runs.has_llm_steps(_AGENT_DOC) is True
    assert runs.has_llm_steps(_DET_DOC) is False
    # nested in a branch arm
    nested = {"steps": [{"id": "b", "branch": {"then": {"steps": [{"id": "x", "prompt": "p"}]}}}]}
    assert runs.has_llm_steps(nested) is True


def test_run_workflow_executes_llm_workflow_inside_gate_session(tmp_path, monkeypatch):
    """An LLM workflow run is wrapped in the snapshot gate session (so its agent steps read
    the gated root, not the mutable checkout). A deterministic workflow is NOT gated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "g"))

    seen: dict = {}

    def _spy(doc, inputs, **kw):
        seen[doc.get("name")] = llmcfg.in_gate_session()

        class _R:
            run_id = kw.get("run_id") or "r"
            workflow_name = doc.get("name")
            status = "succeeded"
            terminal_step = None
            terminal_output = None
            outputs: dict = {}
            steps: dict = {}
            error = None

        return _R()

    monkeypatch.setattr(runs._ex, "run_workflow", _spy)
    # Isolate the GATING decision from workflow-schema validation (a separate concern):
    # return the doc as-is so has_llm_steps drives the gate.
    monkeypatch.setattr(runs, "load_workflow_doc", lambda src, rr=None: src)
    # conftest sets REBAR_GATE_SOURCE=local → local gate (no fetch/materialize needed offline).
    runs.run(_AGENT_DOC, {}, repo_root=str(repo))
    agent_gated = seen["wf"]
    seen.clear()
    runs.run(_DET_DOC, {}, repo_root=str(repo))
    det_gated = seen["wf"]
    assert agent_gated is True, "LLM workflow must execute inside the snapshot gate session"
    assert det_gated is False, "deterministic workflow must NOT pay the snapshot gate"


# --------------------------------------------------------------------------------------
# e95a — the MCP run_workflow tool fences LLM workflows behind REBAR_MCP_ALLOW_LLM
# --------------------------------------------------------------------------------------
def test_mcp_run_workflow_fences_llm_without_allow_llm(rebar_repo, monkeypatch):
    import asyncio

    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    monkeypatch.setenv("REBAR_MCP_READONLY", "0")  # write tools available
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))

    # Stub doc resolution (isolating the FENCE from schema parsing): agent* -> an LLM doc.
    from rebar.llm.workflow import runs as _runs

    monkeypatch.setattr(
        _runs,
        "load_workflow_doc",
        lambda name, rr=None: _AGENT_DOC if "agent" in str(name) else _DET_DOC,
    )

    from rebar.mcp_server import build_server

    srv = build_server()

    async def _call(name, args):
        return await srv.call_tool(name, args)

    # LLM workflow without allow_llm → fenced (ValueError mentioning the gate).
    with pytest.raises(Exception) as exc:
        asyncio.run(_call("run_workflow", {"workflow": "agentwf", "ticket_id": tid}))
    assert "allow_llm" in str(exc.value).lower() or "disabled" in str(exc.value).lower()

    # dry_run is exempt (offline) — must NOT be fenced (returns a run_id).
    res = asyncio.run(
        _call("run_workflow", {"workflow": "agentwf", "ticket_id": tid, "dry_run": True})
    )
    assert res is not None
