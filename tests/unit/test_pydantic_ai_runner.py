"""PydanticAIRunner (7d58): provider-agnostic agent runtime behind the Runner seam —
model-string selection, the three output modes (via an offline FunctionModel, no
billable call), least-privilege tools, runner selection, and FakeRunner non-regression.
"""

from __future__ import annotations

import pytest

from rebar.llm import pai_tools
from rebar.llm.config import RUNNERS, LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import (
    FakeRunner,
    PydanticAIRunner,
    RunRequest,
    _pai_model,
    get_runner,
)

pytest.importorskip("pydantic_ai")


def _function_model(json_out: str):
    """An offline Pydantic AI model that returns a fixed text payload (PromptedOutput
    parses it) — the deterministic, no-token test seam (TestModel doesn't satisfy
    PromptedOutput)."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        return ModelResponse(parts=[TextPart(json_out)])

    return FunctionModel(gen)


def _cfg(**kw):
    return LLMConfig(model=kw.pop("model", "claude-opus-4-8"), repo_path=".", **kw)


# ── Model-string selection (no per-provider code) ──────────────────────────────


def test_model_string_provider_inference():
    assert _pai_model(_cfg(model="claude-opus-4-8")) == "anthropic:claude-opus-4-8"
    assert _pai_model(_cfg(model="gpt-4o")) == "openai:gpt-4o"
    # an explicit provider-qualified string is used verbatim
    assert _pai_model(_cfg(model="anthropic:claude-sonnet-4-6")) == "anthropic:claude-sonnet-4-6"
    assert _pai_model(_cfg(model="google-gla:gemini-2.5-flash")) == "google-gla:gemini-2.5-flash"


# ── Runner selection ───────────────────────────────────────────────────────────


def test_pydantic_ai_is_a_registered_runner():
    assert "pydantic_ai" in RUNNERS


def test_get_runner_selects_pydantic_ai():
    r = get_runner(_cfg(runner="pydantic_ai"))
    assert isinstance(r, PydanticAIRunner) and r.name == "pydantic_ai"


def test_unknown_runner_still_errors():
    with pytest.raises(LLMConfigError, match="unknown runner"):
        get_runner(_cfg(runner="bogus"))


def test_fake_runner_unaffected():
    # The swap must not disturb the offline test seam.
    out = FakeRunner(findings=[], summary="ok").run(
        RunRequest(
            system_prompt="s",
            instructions="i",
            config=_cfg(runner="fake"),
            reviewers=["r"],
            mode="findings",
        )
    )
    assert out["runner"] == "fake"


# ── The three output modes (offline via FunctionModel) ─────────────────────────


def test_findings_mode_returns_review_result():
    runner = PydanticAIRunner(
        _cfg(), model_override=_function_model('{"findings": [], "summary": "looks good"}')
    )
    out = runner.run(
        RunRequest(
            system_prompt="You review.",
            instructions="Review.",
            config=runner._config,
            reviewers=["code-quality"],
            mode="findings",
            output_schema="review_result",
        )
    )
    assert out["runner"] == "pydantic_ai"
    assert isinstance(out.get("findings"), list)


def test_structured_mode_returns_validated_payload():
    runner = PydanticAIRunner(
        _cfg(),
        model_override=_function_model('{"verdict": "PASS", "findings": [], "summary": "met"}'),
    )
    out = runner.run(
        RunRequest(
            system_prompt="x",
            instructions="y",
            config=runner._config,
            reviewers=["v"],
            mode="structured",
            output_schema="completion_verdict",
        )
    )
    assert out["verdict"] == "PASS"
    assert out["runner"] == "pydantic_ai"


def test_text_mode_returns_final_text():
    runner = PydanticAIRunner(_cfg(), model_override=_function_model("just some prose"))
    out = runner.run(
        RunRequest(
            system_prompt="x", instructions="y", config=runner._config, reviewers=["v"], mode="text"
        )
    )
    assert out["text"] == "just some prose"
    assert out["runner"] == "pydantic_ai"


# ── Tools: least privilege + repo-root confinement ─────────────────────────────


def test_filesystem_tools_are_repo_confined(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    read_file, list_directory, search_files = pai_tools.filesystem_tools(str(tmp_path))
    assert "hello" in read_file("a.txt")
    assert "a.txt" in list_directory(".")
    assert "a.txt:1" in search_files("hello")
    # traversal outside the root is refused (surfaced as a tool error, never a read)
    assert read_file("../../../../etc/passwd").startswith("Error")


def test_search_files_includes_github_excludes_state_dirs(tmp_path):
    # Regression for the deny-list boundary bug: `.git` (denied) must NOT prefix-match
    # `.github` (a legitimate dir). `.github` is searched; `.git` is excluded.
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("FINDME here\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("FINDME secret\n", encoding="utf-8")
    _, _, search_files = pai_tools.filesystem_tools(str(tmp_path))
    out = search_files("FINDME")
    assert ".github/ci.yml" in out
    assert ".git/config" not in out


def test_search_files_skips_vendored_noise(tmp_path):
    (tmp_path / "src.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("NEEDLE\n", encoding="utf-8")
    _, _, search_files = pai_tools.filesystem_tools(str(tmp_path))
    out = search_files("NEEDLE")
    assert "src.py" in out
    assert ".venv" not in out  # the discovery filter prunes vendored dirs


def test_unsupported_config_is_a_loud_error():
    # base_url / api_key are dropped by this runner; surfacing them must FAIL, not
    # silently ignore (a capability regression vs the langgraph runner).
    runner = PydanticAIRunner(_cfg(base_url="http://localhost:1234/v1"))
    with pytest.raises(LLMConfigError, match="base_url"):
        runner.preflight()
    runner2 = PydanticAIRunner(_cfg(api_key="sk-local"))
    with pytest.raises(LLMConfigError, match="api_key"):
        runner2.run(
            RunRequest(
                system_prompt="x",
                instructions="y",
                config=runner2._config,
                reviewers=["v"],
                mode="text",
            )
        )


def test_rebar_tools_are_least_privilege():
    read_only = pai_tools.rebar_tools(".", allow_comment=False)
    full = pai_tools.rebar_tools(".", allow_comment=True)
    assert [t.__name__ for t in read_only] == ["show_ticket"]
    assert {t.__name__ for t in full} == {"show_ticket", "comment_ticket"}


def test_mcp_toolsets_empty_and_malformed():
    assert pai_tools.mcp_toolsets({}) == []
    from rebar.llm.errors import LLMRunnerError

    with pytest.raises(LLMRunnerError, match="command|url"):
        pai_tools.mcp_toolsets({"srv": {}})


def test_preflight_ok_with_extra_installed():
    PydanticAIRunner(_cfg()).preflight()  # pydantic-ai-slim is installed in the test env
