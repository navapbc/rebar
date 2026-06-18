"""WS-F2: workflow prompt-ref validation + reviewer template-variable parity gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.workflow import lint as L

# The universe of variables any review op supplies (review_ticket/review_code →
# ticket_id/ticket_context/repo_path; scan_epics_for_spec → spec/epics/repo_path).
_SUPPLIED_UNIVERSE = {"ticket_id", "ticket_context", "spec", "epics", "repo_path"}


def _agent_wf(prompt_id: str) -> dict:
    return {
        "schema_version": "1",
        "name": "t",
        "steps": [{"id": "a", "prompt": prompt_id, "mode": "findings"}],
    }


def test_lint_prompt_refs_flags_unknown() -> None:
    findings = L.lint_prompt_refs(_agent_wf("no-such-prompt"))
    assert any("does not resolve" in f.message for f in findings)


def test_lint_prompt_refs_accepts_catalog_reviewer() -> None:
    assert L.lint_prompt_refs(_agent_wf("code-quality")) == []


def test_lint_prompt_refs_accepts_user_prompt_file(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "my-custom.md").write_text("hi")
    assert L.lint_prompt_refs(_agent_wf("my-custom"), repo_root=str(tmp_path)) == []
    # Without the repo_root (so the file isn't found) it is flagged.
    assert L.lint_prompt_refs(_agent_wf("my-custom")) != []


def test_lint_workflow_check_prompts_opt_in() -> None:
    pytest.importorskip("jsonschema")
    wf = (
        'schema_version: "1"\nname: t\nsteps:\n'
        "  - id: a\n    prompt: bogus-reviewer\n    mode: findings\n"
    )
    # Default: prompt refs NOT checked (broad callers unaffected).
    assert not any("does not resolve" in f.message for f in L.lint_workflow(wf))
    # Opt-in: flagged.
    assert any("does not resolve" in f.message for f in L.lint_workflow(wf, check_prompts=True))


def test_reviewer_template_var_parity() -> None:
    # PARITY GATE: every packaged reviewer's template variables must be drawn from
    # the universe the ops actually supply — a prompt edit that introduces an
    # unsuppliable var fails here.
    catalog = prompts.load_catalog()
    for rid, rv in catalog.items():
        text = prompts.canonical_prompt_text(rv)
        used = prompts.template_variables(text)
        extra = used - _SUPPLIED_UNIVERSE
        assert not extra, f"reviewer {rid!r} template references unsuppliable var(s) {extra}"
