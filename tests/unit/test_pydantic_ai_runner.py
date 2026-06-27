"""PydanticAIRunner (7d58): provider-agnostic agent runtime behind the Runner seam —
model-string selection, the three output modes (via an offline FunctionModel, no
billable call), least-privilege tools, runner selection, and FakeRunner non-regression.
"""

from __future__ import annotations

import pytest

from rebar.llm import pai_tools
from rebar.llm.config import RUNNERS, LLMConfig
from rebar.llm.errors import LLMConfigError, LLMRunnerError
from rebar.llm.runner import (
    FakeRunner,
    PydanticAIRunner,
    RunRequest,
    _anthropic_cache_settings,
    _extract_usage,
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


def _sequence_model(texts):
    """A FunctionModel that returns ``texts[i]`` on the i-th call — exercises the
    bounded-retry path (a near-miss reply followed by a good one)."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state = {"i": 0}

    def gen(messages, info):
        i = min(state["i"], len(texts) - 1)
        state["i"] += 1
        return ModelResponse(parts=[TextPart(texts[i])])

    return FunctionModel(gen), state  # state["i"] == number of model calls


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


def test_structured_path_repairs_near_miss_output():
    # A markdown-fenced, trailing-comma reply (1268 layer 2 json-repair) is recovered
    # deterministically — no second interpreter LLM, no retry needed.
    runner = PydanticAIRunner(
        _cfg(),
        model_override=_function_model('```json\n{"verdict": "PASS", "findings": [],}\n```'),
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


def test_structured_path_bounded_retry_recovers_and_stops_early():
    # First reply is unparseable; the bounded retry (layer 4) feeds the error back and
    # the second reply validates — and the runner STOPS as soon as it validates (exactly
    # 2 model calls here, not the full budget).
    from rebar.llm import structured as _s

    model, calls = _sequence_model(
        ["sorry, I can't produce JSON", '{"verdict": "FAIL", "findings": [], "summary": "no"}']
    )
    runner = PydanticAIRunner(_cfg(), model_override=model)
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
    assert out["verdict"] == "FAIL"
    assert calls["i"] == 2  # recovered on the first retry; did not burn the rest of the budget
    assert calls["i"] <= 1 + _s.OUTPUT_RETRIES


def test_structured_path_exhausts_exactly_the_bounded_budget():
    # An always-unparseable model: the runner makes EXACTLY 1 + OUTPUT_RETRIES attempts
    # (one initial + the bounded retries), then raises — guarding against silent inflation
    # of billable calls.
    from rebar.llm import structured as _s

    model, calls = _sequence_model(["never any json here"])
    runner = PydanticAIRunner(_cfg(), model_override=model)
    with pytest.raises(LLMRunnerError):  # StructuredOutputError is an LLMRunnerError subclass
        runner.run(
            RunRequest(
                system_prompt="x",
                instructions="y",
                config=runner._config,
                reviewers=["v"],
                mode="structured",
                output_schema="completion_verdict",
            )
        )
    assert calls["i"] == 1 + _s.OUTPUT_RETRIES


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
    # silently ignore (they would otherwise be a silent capability gap).
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


def test_mcp_toolsets_builds_stdio_and_http():
    # The happy paths: a `command` config builds a stdio toolset, a `url` config builds an
    # HTTP toolset (one each). Decoupled from the concrete pydantic-ai class (which it
    # deprecates for MCPToolset in v2) — we assert a toolset is built, not its exact type.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # pydantic-ai v2 MCP rename
        stdio = pai_tools.mcp_toolsets({"a": {"command": "echo", "args": ["hi"]}})
        http = pai_tools.mcp_toolsets({"b": {"url": "http://localhost:9/mcp"}})
    assert len(stdio) == 1 and stdio[0] is not None
    assert len(http) == 1 and http[0] is not None


def test_preflight_ok_with_extra_installed():
    PydanticAIRunner(_cfg()).preflight()  # pydantic-ai-slim is installed in the test env


# ── Prompt caching: anthropic gating + usage capture (story 0250) ───────────────


def test_cache_settings_enabled_only_for_anthropic():
    # Anthropic-qualified resolved strings get BOTH cache flags; every other provider
    # gets None (the keys error on openai/gemini), so the call is unchanged there.
    s = _anthropic_cache_settings("anthropic:claude-opus-4-8")
    assert s is not None
    assert s["anthropic_cache_instructions"] is True
    assert s["anthropic_cache_tool_definitions"] is True
    assert _anthropic_cache_settings("openai:gpt-4o") is None
    assert _anthropic_cache_settings("google-gla:gemini-2.5-flash") is None
    assert _anthropic_cache_settings("") is None  # the model_override (test) sentinel


def test_extract_usage_reads_normalized_cache_tokens():
    # pydantic-ai 1.107.0 normalizes Anthropic's raw cache_*_input_tokens to
    # cache_read_tokens / cache_write_tokens on RunUsage.
    class _U:
        input_tokens = 1200
        output_tokens = 50
        cache_read_tokens = 900
        cache_write_tokens = 300

    class _Res:
        def usage(self):
            return _U()

    u = _extract_usage(_Res())
    assert u == {
        "input_tokens": 1200,
        "output_tokens": 50,
        "cache_read_tokens": 900,
        "cache_write_tokens": 300,
    }


def test_extract_usage_is_defensive_on_missing_usage():
    class _NoUsage:
        def usage(self):
            raise RuntimeError("no usage available")

    assert _extract_usage(_NoUsage()) == {}


@pytest.mark.parametrize(
    "resolved,expect_cache",
    [("anthropic:claude-opus-4-8", True), ("openai:gpt-4o", False)],
)
def test_cache_model_settings_attached_only_for_anthropic(monkeypatch, resolved, expect_cache):
    # The wiring proof: run() must attach the cache model_settings to the Agent kwargs
    # ONLY when the resolved provider is anthropic. We stub the structured path to
    # capture the kwargs run() assembled — pydantic-ai's own request mapper then places
    # the cache_control breakpoint on the system-prompt block (anthropic.py:1611-1616).
    from rebar.llm import runner as runner_mod

    captured: dict = {}

    def _fake_structured(Agent, model, resolved_, req, kwargs, usage_limits):
        captured["model_settings"] = kwargs.get("model_settings")
        return {"verdict": "PASS", "findings": [], "summary": "s"}, {}

    monkeypatch.setattr(runner_mod, "_pai_structured", _fake_structured)
    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", lambda: object)
    monkeypatch.setattr(runner_mod, "_pai_model", lambda cfg: resolved)
    monkeypatch.setattr(
        runner_mod._findings,
        "finalize_outcome",
        lambda outcome, **kw: {**outcome["structured_response"]},
    )

    runner = PydanticAIRunner(_cfg())
    runner.run(
        RunRequest(
            system_prompt="sys",
            instructions="ins",
            config=runner._config,
            execution_mode="single_turn",
            mode="structured",
            output_schema="completion_verdict",
        )
    )
    if expect_cache:
        assert captured["model_settings"]["anthropic_cache_instructions"] is True
        assert captured["model_settings"]["anthropic_cache_tool_definitions"] is True
    else:
        assert captured["model_settings"] is None


def test_usage_is_surfaced_on_the_result_dict():
    # An end-to-end (offline) run threads result.usage() onto the returned dict under
    # the private _usage key — the per-run observability hook callers read.
    runner = PydanticAIRunner(
        _cfg(),
        model_override=_function_model('{"verdict": "PASS", "findings": [], "summary": "ok"}'),
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
    assert "_usage" in out
    assert set(out["_usage"]) >= {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }
