"""Agent-build invariants (story sorry-clay-anole, epic jira-reb-687): static guards
against silently-disabled safeguards, as tests + a one-time construction check + a
telemetry warning — never per-call blocking. Offline, no billable call.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm import pai_tools
from rebar.llm import runner as runner_mod
from rebar.llm.errors import LLMConfigError
from rebar.llm.runner import (
    _check_tool_capability,
    _warn_if_zeroed_usage,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_capability_cache():
    runner_mod._TOOL_CAPABILITY_CHECKED.clear()
    yield
    runner_mod._TOOL_CAPABILITY_CHECKED.clear()


# ── Tools actually offered (a #5177 hook-strips-a-tool regression is caught) ──
def test_expected_tools_offered():
    fs = {t.__name__ for t in pai_tools.filesystem_tools(".")}
    assert fs == {"read_file", "list_directory", "search_files"}
    rb = {t.__name__ for t in pai_tools.rebar_tools(".", allow_comment=True)}
    assert "show_ticket" in rb and "comment_ticket" in rb
    rb_noc = {t.__name__ for t in pai_tools.rebar_tools(".", allow_comment=False)}
    assert "comment_ticket" not in rb_noc  # gated off when not allowed


# ── Construction-time capability check (#6186) ────────────────────────────────
def _model(supports):
    return SimpleNamespace(profile=SimpleNamespace(supports_tools=supports))


def test_capability_check_fails_fast():
    """A model OBJECT whose profile says it can't call tools raises fast for a tool-using op."""
    with pytest.raises(LLMConfigError, match="does not support tool calling"):
        _check_tool_capability(_model(False), "anthropic:bad-model")


def test_capability_check_passes_for_tool_capable_model():
    _check_tool_capability(_model(True), "anthropic:claude-sonnet-4-6")  # no raise


def test_capability_check_noop_for_string_model():
    """A provider-string model (OpenAI/Gemini) has no profile object — the check is a safe
    no-op (pydantic-ai builds + validates that model internally)."""
    _check_tool_capability("openai:gpt-4o", "openai:gpt-4o")  # str has no .profile → skip


def test_capability_check_cached():
    """The check reads the model profile ONCE per resolved model string, then short-circuits
    on the cache — never per call."""
    reads = {"n": 0}

    class _Prof:
        @property
        def supports_tools(self):
            reads["n"] += 1
            return True

    model = SimpleNamespace(profile=_Prof())
    _check_tool_capability(model, "anthropic:m")
    _check_tool_capability(model, "anthropic:m")
    _check_tool_capability(model, "anthropic:m")
    assert reads["n"] == 1  # profile read exactly once despite three calls


def test_real_anthropic_model_supports_tools():
    """Belt-and-suspenders: the ACTUAL AnthropicModel rebar builds reports supports_tools
    True (offline, no API call), so the check never false-blocks a healthy anthropic run."""
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    m = AnthropicModel(
        "claude-sonnet-4-6",
        provider=AnthropicProvider(anthropic_client=AsyncAnthropic(api_key="sk-ant-x")),
    )
    assert m.profile.supports_tools is True
    _check_tool_capability(m, "anthropic:claude-sonnet-4-6")  # no raise


# ── usage_limits on run*, not the constructor (#1987) ─────────────────────────
def test_usage_limits_on_run_not_constructor(monkeypatch):
    import pydantic_ai.models
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    ctor_kwargs: dict = {}
    run_kwargs: dict = {}
    real_import = runner_mod._import_pydantic_ai

    def _spy_import():
        RealAgent = real_import()

        class _Spy(RealAgent):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                ctor_kwargs.update(kw)
                super().__init__(*a, **kw)

            def run_sync(self, *a, **kw):
                run_kwargs.update(kw)
                return super().run_sync(*a, **kw)

        return _Spy

    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    cfg = LLMConfig(repo_path=".")
    req = RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text")
    PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    assert "usage_limits" in run_kwargs  # passed on run_sync
    assert "usage_limits" not in ctor_kwargs  # NOT on the constructor (#1987)


# ── Usage-plausibility warning (never blocks) ─────────────────────────────────
def test_usage_plausibility_warns_on_zeroed(caplog):
    with caplog.at_level(logging.WARNING, logger="rebar.llm.runner"):
        _warn_if_zeroed_usage({"requests": 1, "input_tokens": 0, "output_tokens": 0})
    assert any("zeroed/implausible" in r.message for r in caplog.records)


def test_usage_plausibility_silent_on_normal(caplog):
    with caplog.at_level(logging.WARNING, logger="rebar.llm.runner"):
        _warn_if_zeroed_usage({"requests": 1, "input_tokens": 5, "output_tokens": 7})
        _warn_if_zeroed_usage({})  # no request → no warning
    assert not any("zeroed/implausible" in r.message for r in caplog.records)
