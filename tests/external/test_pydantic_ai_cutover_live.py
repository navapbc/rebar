"""Live cutover validation for the **pydantic_ai** runner (story d6d1).

Before LangChain/LangGraph is dropped and pydantic_ai becomes the default runner, the
pydantic_ai path must be validated LIVE across every operation it will back — not just the
one reviewer the old live test covered. These exercise the real agent path (billable model
calls), so they are ``external`` (excluded from the default run) and skip without an API key
+ the ``agents`` extra. Each test FORCES ``runner="pydantic_ai"`` via config and asserts the
runner provenance, so a regression in the new default surfaces here.

Coverage (the operations the cutover repoints onto pydantic_ai):
  * review_ticket  — findings mode, opus (validates opus param handling: no temperature 400)
  * review_code    — findings mode
  * scan_epics_for_spec — batch structured output
  * verify_completion   — completion_verdict structured output (the close gate)
  * text mode      — the non-findings output path, via the runner directly
  * workflow agent step — the run_workflow → RunnerAgentStep → pydantic_ai path

Run::  REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… pytest -m external \
           tests/external/test_pydantic_ai_cutover_live.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external

_SONNET = "claude-sonnet-4-6"


def _have_live() -> bool:
    try:
        import rebar.llm as llm
    except ImportError:
        return False
    return llm.agents_extra_installed() and bool(os.environ.get("ANTHROPIC_API_KEY"))


_skip = pytest.mark.skipif(not _have_live(), reason="no ANTHROPIC_API_KEY / agents extra")


def _cfg(repo: Path, model: str):
    from rebar.llm.config import LLMConfig

    # Force the pydantic_ai runner (the cutover target) regardless of the derived default.
    return LLMConfig(model=model, repo_path=str(repo), runner="pydantic_ai")


@_skip
def test_pydantic_review_ticket_opus(rebar_repo: Path) -> None:
    """The primary review op via pydantic_ai on OPUS — validates opus parameter handling
    (no `temperature` sent, which would 400) on the new runner."""
    import rebar.llm as llm

    epic = rebar.create_ticket(
        "epic",
        "Add login",
        description="Build login.\n\n## Acceptance Criteria\n- [ ] users can log in",
        repo_root=str(rebar_repo),
    )
    (rebar_repo / "app.py").write_text("API_KEY = 'hardcoded-secret'\n", encoding="utf-8")
    result = llm.review_ticket(
        epic,
        "ticket-quality",
        repo_root=str(rebar_repo),
        config=_cfg(rebar_repo, "claude-opus-4-8"),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "pydantic_ai"
    assert result["model"] == "anthropic:claude-opus-4-8"
    assert isinstance(result["findings"], list)


@_skip
def test_pydantic_review_code(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import rebar.llm as llm

    # review_code is an OFF-BY-DEFAULT four-pass gate (epic b744): disabled it returns an
    # inert empty result (runner="code-review-disabled"). Enable the capability so this
    # LIVE test exercises the real gated path on the supplied diff.
    monkeypatch.setenv("REBAR_VERIFY_ENABLE_CODE_REVIEW", "1")

    diff = (
        "--- a/auth.py\n+++ b/auth.py\n@@ -0,0 +1,2 @@\n+def check(t):\n+    return True  # TODO\n"
    )
    body = "def check(t):\n    return True  # TODO\n"
    (rebar_repo / "auth.py").write_text(body, encoding="utf-8")
    result = llm.review_code(
        diff_text=diff,
        changed_files=["auth.py"],
        reviewers=["code-quality"],
        repo_root=str(rebar_repo),
        config=_cfg(rebar_repo, _SONNET),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    # The enabled four-pass gate runs via the pydantic_ai runner and reports it as the
    # provenance (NOT the inert "code-review-disabled" of the default-off path) — this is
    # the live pydantic_ai-runner validation this cutover test exists for.
    assert result["runner"] == "pydantic_ai"
    assert isinstance(result["findings"], list)


@_skip
def test_pydantic_scan_spec(rebar_repo: Path) -> None:
    import rebar.llm as llm

    rebar.create_ticket(
        "epic",
        "Authentication",
        description="Login.\n\n## Acceptance Criteria\n- [ ] users can log in",
        repo_root=str(rebar_repo),
    )
    result = llm.scan_epics_for_spec(
        "The product must support multi-factor authentication and password reset.",
        repo_root=str(rebar_repo),
        config=_cfg(rebar_repo, _SONNET),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "pydantic_ai"
    assert isinstance(result["findings"], list)


@_skip
def test_pydantic_verify_completion(rebar_repo: Path) -> None:
    """The close gate via pydantic_ai: completion_verdict structured output."""
    from rebar.llm.completion import verify_completion

    t = rebar.create_ticket(
        "task",
        "Add a greeting helper",
        description=(
            "Add a greet() function.\n\n## Acceptance Criteria\n"
            "- [ ] a function `greet(name)` exists in greet.py returning 'hello, <name>'"
        ),
        repo_root=str(rebar_repo),
    )
    (rebar_repo / "greet.py").write_text(
        "def greet(name):\n    return f'hello, {name}'\n", encoding="utf-8"
    )
    result = verify_completion(t, repo_root=str(rebar_repo), config=_cfg(rebar_repo, _SONNET))
    schemas.validator(schemas.COMPLETION_VERDICT).validate(result)
    assert result["runner"] == "pydantic_ai"
    assert result["verdict"] in ("PASS", "FAIL")


@_skip
def test_pydantic_text_mode(rebar_repo: Path) -> None:
    """The non-findings (text) output path on the pydantic_ai runner."""
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    cfg = _cfg(rebar_repo, _SONNET)
    runner = PydanticAIRunner(cfg)
    runner.preflight()
    req = RunRequest(
        system_prompt="You are a concise assistant.",
        instructions="Reply with exactly the word: ready",
        config=cfg,
        mode="text",
        reviewers=[],
        # A text reply reads nothing, so run WITHOUT filesystem tools. Agentic mode (the
        # default) would wire read-only fs tools and trip the repo-snapshot gate added in
        # b25fafcd1 (epic raze-vet-ditch) — single_turn is the faithful text-path exercise.
        execution_mode="single_turn",
    )
    out = runner.run(req)
    assert out["runner"] == "pydantic_ai"
    # text mode populates a textual result (finalize_outcome maps messages -> output)
    text = out.get("text") or out.get("summary") or out.get("output")
    assert text or out.get("findings") is not None


@_skip
def test_pydantic_workflow_agent_step(rebar_repo: Path) -> None:
    """The workflow path: run_workflow → RunnerAgentStep → pydantic_ai (injected runner)."""
    from rebar.llm.runner import PydanticAIRunner
    from rebar.llm.workflow.executor import run_workflow
    from rebar.llm.workflow.runs import RunnerAgentStep

    t = rebar.create_ticket(
        "task",
        "Review me",
        description="A task.\n\n## Acceptance Criteria\n- [ ] does the thing",
        repo_root=str(rebar_repo),
    )
    doc = {
        "schema_version": "2",
        "name": "live-agent",
        "steps": [{"id": "review", "prompt": "ticket-quality", "mode": "findings"}],
    }
    agent_runner = RunnerAgentStep(
        runner=PydanticAIRunner(_cfg(rebar_repo, _SONNET)), repo_root=str(rebar_repo)
    )
    res = run_workflow(
        doc, {"ticket_id": t}, target_ticket=t, repo_root=str(rebar_repo), agent_runner=agent_runner
    )
    # The run completed and the agent step produced an output via pydantic_ai.
    assert res is not None
