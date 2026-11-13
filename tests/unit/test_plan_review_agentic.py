"""The agentic plan-review engine (ADR 0123): request shape, read-only tool surface, the
prompt, the interim verdict, failure handling and the pipeline-only post-dispatch tail.

Offline: every review runs on a counting FakeRunner and the read-only surface is checked on
the real runner with a pydantic-ai FunctionModel, so no model call is billed.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
import rebar.llm
from rebar.llm.plan_review import agentic
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_PASSING_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
)
_FINDING = {
    "id": "F1",
    "title": "Wrong file",
    "description": "The plan edits a file the change does not live in.",
    "severity": "blocker",
    "evidence": "src/thing.py has no such function.",
    "suggested_fix": "Name the module that defines it.",
    "criteria": ["E4"],
}


class _AgentFake(FakeRunner):
    """Records every request; answers the agentic contract with canned findings, or raises."""

    name = "fake"

    def __init__(self, findings=None, *, error: Exception | None = None) -> None:
        super().__init__()
        self.requests: list = []
        self._agent_findings = findings if findings is not None else [dict(_FINDING)]
        self._error = error

    def run(self, req) -> dict:  # type: ignore[override]
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        if req.output_schema == agentic.FINDINGS_CONTRACT:
            payload = {"findings": [dict(f) for f in self._agent_findings]}
        else:
            payload = {"analysis": "", "findings": []}
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.delenv("REBAR_PLAN_REVIEW_BUDGET", raising=False)
    rebar.init_repo(repo_root=str(repo))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _ticket(repo: Path, desc: str = _PASSING_DESC) -> str:
    nonce = f"\nMaterial nonce (test isolation): {uuid.uuid4().hex}.\n"
    return rebar.create_ticket(
        "task", "agentic plan", description=desc + nonce, repo_root=str(repo)
    )


def _review(repo: Path, tid: str, runner, **kw):
    return rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo), engine="agentic", **kw)


# ── request shape ───────────────────────────────────────────────────────────────
def test_agent_request_shape(repo: Path) -> None:
    from rebar.llm.model_classes import resolve_model_string

    tid = _ticket(repo)
    runner = _AgentFake()
    verdict = _review(repo, tid, runner)

    assert len(runner.requests) == 1
    req = runner.requests[0]
    assert req.execution_mode == "agentic"
    assert req.mode == "structured"
    assert req.output_schema == "plan_review_agentic_findings"
    assert (req.tool_step_limit, req.iteration_limit, req.read_only) == (60, 156, True)
    assert req.config.model == resolve_model_string("frontier", req.config.repo_path)
    assert "change a thing in `src/thing.py`" in req.instructions  # the plan reached the agent
    assert "@@" not in req.system_prompt + req.instructions

    # _FINDING is a blocker, so the adapted verdict BLOCKs and is not signed.
    assert verdict["verdict"] == "BLOCK"
    assert verdict["signature"]["signed"] is False
    assert verdict["coverage"]["engine"] == "agentic"
    assert rebar.llm.plan_review_status(tid, repo_root=str(repo))["verdict"] == "unsigned"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("provider exploded"), ValueError("agent output failed schema")],
    ids=["runner-error", "schema-invalid"],
)
def test_a_failed_agent_run_is_unsigned_indeterminate(repo: Path, error: Exception) -> None:
    tid = _ticket(repo)
    verdict = _review(repo, tid, _AgentFake(error=error))
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["signature"]["signed"] is False
    assert rebar.llm.plan_review_status(tid, repo_root=str(repo))["verdict"] == "unsigned"


def test_floor_blocked_ticket_makes_zero_agent_calls(repo: Path) -> None:
    desc = (
        "## What\nchange a thing in `src/thing.py`.\n\n## Why\nbecause.\n\n"
        "## Verification\n`pytest tests/unit` proves the change.\n"
    )
    runner = _AgentFake()
    verdict = _review(repo, _ticket(repo, desc), runner)
    assert verdict["verdict"] == "BLOCK"
    assert runner.requests == []


# ── the post-dispatch tail is pipeline-only ─────────────────────────────────────
def test_post_dispatch_tail_runs_only_under_pipeline(repo: Path, monkeypatch) -> None:
    import rebar.llm.plan_review as plan_review

    calls: list[str] = []
    monkeypatch.setattr(plan_review, "_apply_review_floors", lambda *a, **kw: calls.append("tail"))
    floor_desc = "## What\nchange a thing.\n\n## Why\nbecause.\n"
    for engine in ("agentic", "pipeline"):
        calls.clear()
        rebar.llm.review_plan(
            _ticket(repo, floor_desc), runner=_AgentFake(), repo_root=str(repo), engine=engine
        )
        assert calls == ([] if engine == "agentic" else ["tail"])
    calls.clear()
    _review(repo, _ticket(repo), _AgentFake())  # DET-passing agentic (interim verdict)
    _review(repo, _ticket(repo), _AgentFake(error=RuntimeError("boom")))  # degraded
    assert calls == []


# ── the inputs op + prompt ──────────────────────────────────────────────────────
def test_inputs_render_plan_prerequisites_and_llm_criteria() -> None:
    out = agentic.plan_review_agentic_inputs(
        SimpleNamespace(
            inputs={
                "subject_plan": "SUBJECT",
                "prerequisites": [{"canonical_id": "aaaa-bbbb", "rendered_text": "PREREQ"}],
            },
            repo_root=None,
        )
    )
    assert out["plan"].startswith("SUBJECT")
    assert '<prerequisite id="aaaa-bbbb">\nPREREQ\n</prerequisite>' in out["plan"]
    ids = {line.split(" ", 2)[1] for line in out["criteria"].splitlines()}
    assert "A1" in ids
    assert not ids & {"P1", "P10", "prerequisite-consistency"}


def test_prompt_is_packaged_and_free_of_bakeoff_placeholders() -> None:
    from rebar.llm.prompting import prompts

    prompt = prompts.get_prompt("plan-review-agentic")
    assert prompt.execution_mode == "agentic"
    system, instructions, _ = prompts.resolve_prompt_cached(
        prompt,
        {"plan": "THE-PLAN", "criteria": "- A1 — x", "ticket_id": "", "ticket_context": ""},
        base_instructions="",
        langfuse_cfg=None,
        repo_root=None,
    )
    text = system + instructions
    assert "THE-PLAN" in instructions and "- A1 — x" in instructions
    assert "@@" not in text and "plan-review-bakeoff" not in text


# ── read-only tool surface on the real runner ───────────────────────────────────
def _offered_tools(monkeypatch, *, read_only: bool) -> tuple[set[str], list]:
    """The tool names and MCP toolsets the real runner hands the agent for one request."""
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm import pai_tools
    from rebar.llm import runner as runner_mod
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    seen: dict = {}
    real_build = runner_mod.build_agent_kwargs

    def _spy(cfg, req, tools, toolsets, **kw):
        seen["tools"], seen["toolsets"] = list(tools), list(toolsets)
        return real_build(cfg, req, tools, [], **kw)  # the sentinel never reaches pydantic-ai

    sentinel = object()
    monkeypatch.setattr(runner_mod, "build_agent_kwargs", _spy)
    monkeypatch.setattr(pai_tools, "mcp_toolsets", lambda servers: [sentinel])

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("done")])

    cfg = LLMConfig(repo_path=".")
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=["v"],
        mode="text",
        read_only=read_only,
    )
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    return {t.__name__ for t in seen["tools"]}, seen["toolsets"]


def test_read_only_request_gets_only_read_tools(monkeypatch) -> None:
    names, toolsets = _offered_tools(monkeypatch, read_only=True)
    assert names == {"read_file", "list_directory", "search_files", "resolve_symbol", "show_ticket"}
    assert toolsets == []


def test_default_request_keeps_comment_and_mcp_tools(monkeypatch) -> None:
    names, toolsets = _offered_tools(monkeypatch, read_only=False)
    assert "comment_ticket" in names
    assert len(toolsets) == 1


# ── the real runner reports code reads on _usage (bug bridal-gifted-vampirebat) ──────────
def _usage_of_run(tmp_path: Path, monkeypatch, *, read_first: bool) -> dict:
    """``_usage`` of one read-only run on the real runner; the model optionally reads a file."""
    pytest.importorskip("pydantic_ai")
    import pydantic_ai.models
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)
    (tmp_path / "thing.py").write_text("VALUE = 1\n")
    turns = {"n": 0}

    def gen(messages, info):
        turns["n"] += 1
        if read_first and turns["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="read_file", args={"path": "thing.py"})]
            )
        return ModelResponse(parts=[TextPart("done")])

    cfg = LLMConfig(repo_path=str(tmp_path))
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=["v"],
        mode="text",
        read_only=True,
    )
    return PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)["_usage"]


def test_real_runner_reports_no_code_read_without_a_read(tmp_path: Path, monkeypatch) -> None:
    usage = _usage_of_run(tmp_path, monkeypatch, read_first=False)
    assert usage["tool_calls"] == 0
    assert usage["distinct_fetches"] == []


def test_real_runner_reports_a_read_file_call(tmp_path: Path, monkeypatch) -> None:
    usage = _usage_of_run(tmp_path, monkeypatch, read_first=True)
    assert usage["tool_calls"] == 1
    assert [f["tool"] for f in usage["distinct_fetches"]] == ["read_file"]
