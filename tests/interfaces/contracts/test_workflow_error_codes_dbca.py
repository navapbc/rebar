"""Held-out regression oracle for dbca-97ac-ad96-4d6d.

The workflow-engine caller-input / not-found failures reached the MCP read tools
(`render_workflow`, `get_workflow_status`) as the single code `llm_unavailable`, because
`WorkflowError`/`WorkflowParseError` subclass `LLMError` and `error_code_for` blanket-mapped
every `LLMError` to `llm_unavailable`. These tests pin the corrected per-subclass taxonomy
through the ACTUAL MCP tools and assert on the machine-readable `error` CODE (never the
message text), covering AC1-AC4.

Assertions target observable envelope output, not private names, so a behaviour-preserving
refactor of the classifier does not break them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[3]


def _clean_env(root: Path) -> dict:
    env = subprocess_env(REBAR_ROOT=str(root))
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        env.pop(var, None)
    return env


def _fresh_tracker(tmp: Path) -> Path:
    env = _clean_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
    subprocess.run(["rebar", "init"], cwd=tmp, check=True, capture_output=True, env=env)
    return tmp


def _mcp_envelope_for(tool: str, args: dict, root: Path) -> dict:
    """Call an MCP tool that is expected to fail and return its structured error envelope."""
    from mcp.server.fastmcp.exceptions import ToolError

    os.environ["REBAR_ROOT"] = str(root)
    for var in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
        os.environ.pop(var, None)
    from rebar.mcp_server import build_server

    with pytest.raises(ToolError) as ei:
        asyncio.run(build_server().call_tool(tool, args))
    return ei.value.__cause__.envelope


# ── AC1: render_workflow unknown NAME -> not_found (through the real read tool) ──────────
def test_render_workflow_unknown_name_is_not_found(tmp_path: Path) -> None:
    import rebar

    root = _fresh_tracker(tmp_path)
    env = _mcp_envelope_for("render_workflow", {"workflow": "nonexistent-wf"}, root)
    assert env["error"] == "not_found"
    assert env["error"] != "llm_unavailable"
    assert env["error"] in rebar.KNOWN_ERROR_CODES


# ── AC2: get_workflow_status unknown run_id -> not_found (through the real read tool) ─────
def test_get_workflow_status_unknown_run_is_not_found(tmp_path: Path) -> None:
    import rebar

    root = _fresh_tracker(tmp_path)
    env = _mcp_envelope_for("get_workflow_status", {"run_id": "nonexistent-run"}, root)
    assert env["error"] == "not_found"
    assert env["error"] != "llm_unavailable"
    assert env["error"] in rebar.KNOWN_ERROR_CODES


# ── A malformed (found) workflow file is a caller-input parse error -> invalid_input ─────
def test_render_workflow_malformed_is_invalid_input(tmp_path: Path) -> None:
    import rebar

    root = _fresh_tracker(tmp_path)
    wf_dir = root / ".rebar" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    # A bare scalar is not a single mapping document -> WorkflowParseError (parse, found file).
    (wf_dir / "badwf.yaml").write_text("just-a-scalar\n", encoding="utf-8")

    env = _mcp_envelope_for("render_workflow", {"workflow": "badwf"}, root)
    assert env["error"] == "invalid_input"
    assert env["error"] != "llm_unavailable"
    assert env["error"] in rebar.KNOWN_ERROR_CODES


# ── AC3: the EXECUTE base (bare WorkflowError) and genuine outages still map to
#         llm_unavailable — the reclassification must NOT flip the base. ─────────────────
def test_execute_base_and_outage_still_llm_unavailable() -> None:
    import rebar
    from rebar.llm.errors import LLMError, LLMUnavailableError, WorkflowError

    # The bare WorkflowError base is the workflow EXECUTE base; a step can genuinely fail on
    # LLM unavailability, so it must remain llm_unavailable.
    assert rebar.error_code_for(WorkflowError("execute step failed: provider overloaded")) == (
        "llm_unavailable"
    )
    assert rebar.error_code_for(LLMUnavailableError("no API key")) == "llm_unavailable"
    assert rebar.error_code_for(LLMError("generic llm failure")) == "command_failed"


def test_expression_error_is_invalid_input() -> None:
    import rebar
    from rebar._mcp_llm import _structured_llm_failure
    from rebar.llm.workflow.executor import ExpressionError

    exc = ExpressionError("input 'missing' is not set for this run")
    assert rebar.error_code_for(exc) == "invalid_input"
    assert _structured_llm_failure(exc)["error"] == "invalid_input"


def test_editor_missing_bundle_is_command_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import rebar
    from rebar.llm.errors import WorkflowAssetsUnavailableError
    from rebar.llm.workflow import editor

    monkeypatch.setattr(editor, "assets_available", lambda: False)
    with pytest.raises(WorkflowAssetsUnavailableError) as caught:
        editor.edit_workflow(tmp_path / "workflow.yaml", open_browser=False, serve_forever=False)
    assert "editor front-end bundle is missing" in str(caught.value)
    assert rebar.error_code_for(caught.value) == "command_failed"
    assert rebar.error_code_for(caught.value) != "llm_unavailable"


# ── AC3 (second site): _structured_llm_failure keeps genuine outages as llm_unavailable ──
def test_structured_llm_failure_outage_still_llm_unavailable() -> None:
    import rebar
    from rebar._mcp_llm import _structured_llm_failure
    from rebar.llm.errors import LLMUnavailableError

    out = _structured_llm_failure(LLMUnavailableError("provider down"))
    assert out["error"] == "llm_unavailable"
    assert out["error"] in rebar.KNOWN_ERROR_CODES
    # the disposition fields agents branch on are preserved
    assert "resolution_class" in out
    assert "retryable" in out


# ── AC2 (distinct path): a run absent from an EXISTING ticket -> not_found. This is the
#         status()/result() site (runs.py), a different code path from the unknown-run_id
#         lookup miss in _locate — the ticket resolves, but it carries no such run. ────────
def test_get_workflow_status_run_absent_from_ticket_is_not_found(tmp_path: Path) -> None:
    import rebar

    root = _fresh_tracker(tmp_path)
    env = _clean_env(root)
    created = subprocess.run(
        ["rebar", "create", "task", "holds no workflow run", "--output", "json"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    import json as _json

    ticket_id = _json.loads(created.stdout)["id"]
    envelope = _mcp_envelope_for(
        "get_workflow_status",
        {"run_id": "no-such-run", "ticket_id": ticket_id},
        root,
    )
    assert envelope["error"] == "not_found"
    assert envelope["error"] != "llm_unavailable"
    assert envelope["error"] in rebar.KNOWN_ERROR_CODES


# ── invalid_input covers the whole found-but-unusable subtree, not just parse. A lint
#         (validation) failure and a too-new schema_version are both caller-input faults. ─
def test_workflow_validation_and_version_errors_are_invalid_input() -> None:
    import rebar
    from rebar.llm.errors import WorkflowValidationError, WorkflowVersionError

    assert rebar.error_code_for(WorkflowValidationError(["step 'x' missing 'uses'"])) == (
        "invalid_input"
    )
    assert rebar.error_code_for(WorkflowVersionError("schema_version 99 is too new")) == (
        "invalid_input"
    )


# ── AC4: the new vocabulary members exist and every emitted code is a member ─────────────
def test_new_codes_are_known() -> None:
    import rebar

    assert "not_found" in rebar.KNOWN_ERROR_CODES
    assert "invalid_input" in rebar.KNOWN_ERROR_CODES


def test_workflow_not_found_classifies_as_not_found() -> None:
    import rebar
    from rebar.llm.errors import WorkflowNotFoundError

    assert rebar.error_code_for(WorkflowNotFoundError("unknown run_id 'x'")) == "not_found"
