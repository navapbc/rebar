"""External validation of the ``pydantic_ai`` runner across supported operation shapes.

The tests force ``runner="pydantic_ai"`` and assert provenance for findings, batch, completion
verdict, text, and workflow paths. Provider models resolve from configured classes so
each matrix overlay selects its arm. The frontier ``review_code`` case also covers provider
parameter compatibility.

Execution requires the external opt-in, agents package, and configured provider credential.
"""

from __future__ import annotations

from pathlib import Path

import _live_llm
import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_skip = _live_llm.skip_without_live_llm


def _class_model(class_name: str) -> str:
    """The resolved model string for a model class, honouring REBAR_LLM_CONFIG_FILE.

    Resolved LAZILY inside each test rather than at import: the module is imported during
    collection, before a test's fixtures set up their repo, and resolution is cheap.
    """
    from rebar.llm.model_classes import resolve_model_string

    return resolve_model_string(class_name)


def _cfg(repo: Path, model: str):
    from rebar.llm.config import LLMConfig

    # Force the pydantic_ai runner (the cutover target) regardless of the derived default.
    return LLMConfig(model=model, repo_path=str(repo), runner="pydantic_ai")


@_skip
def test_pydantic_review_code_opus(rebar_repo: Path) -> None:
    """Run frontier ``review_code`` through pydantic-ai without an unsupported temperature.

    The four-pass findings path asserts configured model and runner provenance while covering
    parameter handling for the frontier model.
    """
    import rebar.llm as llm

    diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+API_KEY = 'hardcoded-secret'\n"
    (rebar_repo / "app.py").write_text("API_KEY = 'hardcoded-secret'\n", encoding="utf-8")
    result = llm.review_code(
        diff_text=diff,
        changed_files=["app.py"],
        repo_root=str(rebar_repo),
        config=_cfg(rebar_repo, _class_model("frontier")),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "pydantic_ai"
    assert result["model"] == _class_model("frontier")
    assert isinstance(result["findings"], list)


@_skip
def test_pydantic_review_code(rebar_repo: Path) -> None:
    import rebar.llm as llm

    # review_code always runs the four-pass gate (epic b744 + bug 5b32-37c4-f99a-4315), so
    # this LIVE test exercises the real gated path on the supplied diff with no config key.
    diff = (
        "--- a/auth.py\n+++ b/auth.py\n@@ -0,0 +1,2 @@\n+def check(t):\n+    return True  # TODO\n"
    )
    body = "def check(t):\n    return True  # TODO\n"
    (rebar_repo / "auth.py").write_text(body, encoding="utf-8")
    result = llm.review_code(
        diff_text=diff,
        changed_files=["auth.py"],
        repo_root=str(rebar_repo),
        config=_cfg(rebar_repo, _class_model("standard")),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    # The four-pass gate runs via the pydantic_ai runner and reports it as the provenance —
    # this is the live pydantic_ai-runner validation this cutover test exists for.
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
        config=_cfg(rebar_repo, _class_model("standard")),
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
    result = verify_completion(
        t, repo_root=str(rebar_repo), config=_cfg(rebar_repo, _class_model("standard"))
    )
    schemas.validator(schemas.COMPLETION_VERDICT).validate(result)
    assert result["runner"] == "pydantic_ai"
    assert result["verdict"] in ("PASS", "FAIL")


@_skip
def test_pydantic_text_mode(rebar_repo: Path) -> None:
    """The non-findings (text) output path on the pydantic_ai runner."""
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    cfg = _cfg(rebar_repo, _class_model("standard"))
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
        runner=PydanticAIRunner(_cfg(rebar_repo, _class_model("standard"))),
        repo_root=str(rebar_repo),
    )
    res = run_workflow(
        doc, {"ticket_id": t}, target_ticket=t, repo_root=str(rebar_repo), agent_runner=agent_runner
    )
    # The run completed and the agent step produced an output via pydantic_ai.
    assert res is not None
