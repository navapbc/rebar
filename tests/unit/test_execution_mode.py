"""Story 4b2f: prompt-level ``execution_mode`` (single_turn vs agentic), its runner
dispatch, the inspector's agent-step contract view, and override-vs-built-in outputs
drift.

``execution_mode`` is a PROMPT-level concern (how the runner drives the model),
DISTINCT from a workflow step's ``mode`` (output shaping). These are offline tests
(FakeRunner / pure dispatch inspection) — the live single_turn path lives in
``tests/external/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.prompts import PromptError


# ── 1. execution_mode enum + default ──────────────────────────────────────────
def test_execution_mode_defaults_to_agentic_when_absent(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    # A built-in id with an override that omits execution_mode → defaults to agentic.
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\n---\nBODY {{ticket_id}}",
        encoding="utf-8",
    )
    p = prompts.get_prompt("ticket-quality", repo_root=str(tmp_path))
    assert p.execution_mode == "agentic"


def test_execution_mode_rejects_invalid_value(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\nexecution_mode: bogus\n---\nBODY {{ticket_id}}",
        encoding="utf-8",
    )
    with pytest.raises(PromptError) as exc:
        prompts.get_prompt("ticket-quality", repo_root=str(tmp_path))
    assert "execution_mode" in str(exc.value)


@pytest.mark.parametrize("mode", ["single_turn", "agentic"])
def test_execution_mode_accepts_both_valid_values(tmp_path: Path, mode: str) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text(
        f"---\ncategory: review\nexecution_mode: {mode}\noutputs: review_result\n"
        "---\nB {{ticket_id}}",
        encoding="utf-8",
    )
    p = prompts.get_prompt("ticket-quality", repo_root=str(tmp_path))
    assert p.execution_mode == mode


def test_execution_modes_constant_is_closed() -> None:
    assert prompts.EXECUTION_MODES == ("single_turn", "agentic")


# ── 2. RunRequest construction dispatch (build_agent_request) ──────────────────
class _Ctx:
    """Minimal StepContext stand-in for inspecting build_agent_request's decision."""

    def __init__(self, step: dict, inputs: dict | None = None) -> None:
        self.step = step
        self.inputs = inputs or {}
        self.target_ticket = ""


def _prompt(execution_mode: str, *, outputs=None):
    return prompts.Prompt(
        id="p1",
        text="body {{ticket_id}}",
        execution_mode=execution_mode,
        outputs=outputs,
        dimension="d",
    )


def test_build_agent_request_single_turn_sets_structured_and_prompt_outputs() -> None:
    from rebar.llm.workflow.runs import build_agent_request

    # Step asks for findings, but a single_turn prompt OVERRIDES to structured +
    # the PROMPT's outputs contract (not the step's).
    ctx = _Ctx({"prompt": "p1", "mode": "findings", "output_schema": "ignored"})
    req = build_agent_request(
        _prompt("single_turn", outputs="completion_verdict"),
        ctx,
        None,  # cfg unused by the dispatch decision
        system_prompt="sys",
        instructions="ins",
        langfuse_prompt=None,
        ticket_id="t1",
    )
    assert req.execution_mode == "single_turn"
    assert req.mode == "structured"
    assert req.output_schema == "completion_verdict"


def test_build_agent_request_single_turn_requires_outputs() -> None:
    from rebar.llm.workflow.runs import build_agent_request

    ctx = _Ctx({"prompt": "p1"})
    with pytest.raises(PromptError) as exc:
        build_agent_request(
            _prompt("single_turn", outputs=None),
            ctx,
            None,  # cfg unused by the dispatch decision
            system_prompt="sys",
            instructions="ins",
            langfuse_prompt=None,
            ticket_id="t1",
        )
    assert "p1" in str(exc.value)
    assert "outputs" in str(exc.value)


def test_build_agent_request_agentic_honors_step_mode_and_schema() -> None:
    from rebar.llm.workflow.runs import build_agent_request

    ctx = _Ctx({"prompt": "p1", "mode": "findings", "output_schema": "review_result"})
    req = build_agent_request(
        _prompt("agentic"),
        ctx,
        None,  # cfg unused by the dispatch decision
        system_prompt="sys",
        instructions="ins",
        langfuse_prompt=None,
        ticket_id="t1",
    )
    assert req.execution_mode == "agentic"
    assert req.mode == "findings"
    assert req.output_schema == "review_result"


# NOTE: the end-to-end single_turn dispatch (offline via FakeRunner, needs a real
# rebar store) lives in tests/interfaces/store/test_execution_mode_dispatch.py — the
# `rebar_repo` fixture is only available under the interface tier.


# ── 3. Inspector surfaces an agent step's prompt contract ──────────────────────
def test_resolve_contracts_surfaces_agent_prompt_contract(tmp_path: Path) -> None:
    pytest.importorskip("jsonschema")
    from rebar.llm.workflow import editor

    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "with-contract.md").write_text(
        "---\ndescription: A contracted prompt\noutputs: completion_verdict\n"
        "execution_mode: single_turn\n---\nBody {{ticket_id}}",
        encoding="utf-8",
    )
    doc = {"steps": [{"id": "s", "prompt": "with-contract"}]}
    views = editor.resolve_contracts(doc, repo_root=str(tmp_path))
    assert "with-contract" in views
    view = views["with-contract"]
    assert view["has_contract"] is True
    assert view["description"] == "A contracted prompt"
    assert view["produces"]  # completion_verdict has properties


def test_resolve_contracts_agent_prompt_without_contract_is_empty(tmp_path: Path) -> None:
    from rebar.llm.workflow import editor

    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "bare.md").write_text("---\n---\nBody {{ticket_id}}", encoding="utf-8")
    doc = {"steps": [{"id": "s", "prompt": "bare"}]}
    views = editor.resolve_contracts(doc, repo_root=str(tmp_path))
    assert views["bare"]["has_contract"] is False


def test_prompt_contract_view_unknown_prompt_degrades_to_empty() -> None:
    from rebar.llm.workflow import editor

    view = editor.prompt_contract_view("no-such-prompt-anywhere")
    assert view["has_contract"] is False


# ── 4. validate flags override-vs-built-in outputs drift ───────────────────────
def test_prompt_override_drift_flags_changed_outputs(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    # ticket-quality is a built-in with NO outputs front-matter; declaring one is drift.
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\noutputs: review_result\n---\n"
        "Body {{ticket_id}}",
        encoding="utf-8",
    )
    findings = prompts.prompt_override_drift(repo_root=str(tmp_path))
    assert findings
    assert "ticket-quality" in findings[0]
    assert "outputs" in findings[0]


def test_prompt_override_drift_identical_override_is_clean(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    # An override that does NOT touch outputs (same as built-in: absent) → clean.
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\n---\nBody {{ticket_id}}",
        encoding="utf-8",
    )
    assert prompts.prompt_override_drift(repo_root=str(tmp_path)) == []


def test_prompt_override_drift_no_repo_root_is_empty() -> None:
    assert prompts.prompt_override_drift() == []


def test_lint_workflow_surfaces_override_drift(tmp_path: Path) -> None:
    pytest.importorskip("jsonschema")
    from rebar.llm.workflow.lint import lint_workflow

    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\noutputs: review_result\n---\n"
        "Body {{ticket_id}}",
        encoding="utf-8",
    )
    text = "schema_version: '1'\nname: wf\nsteps:\n  - id: s\n    uses: noop\n    with: {}\n"
    findings = lint_workflow(text, repo_root=str(tmp_path))
    drift = [f for f in findings if "outputs contract" in f.message]
    assert drift
    assert all(f.severity == "error" for f in drift)
