"""Server-side web-search gating for AGENT criteria (bug ff64-ca12-7132-40e7).

The T1 prior-art rubric mandates web grounding, but the agent toolset offers no web
access. Fix: a criterion's routing entry may declare ``"web": true``; when the resolved
provider is anthropic, the runner attaches Anthropic's SERVER-SIDE web-search tool to
that request via pydantic-ai's capability mechanism (``Agent(capabilities=[WebSearch()])``
— the supported successor of ``builtin_tools``). Non-anthropic providers and unflagged
criteria are byte-identical to before. No test makes a live web/model call.
"""

from __future__ import annotations

import json
from importlib import resources
from types import SimpleNamespace

import pytest

from rebar.llm.anthropic_model import _anthropic_web_search_capabilities
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")


# ── the anthropic-gated capability helper (settings assembly seam) ─────────────


def test_web_capability_emitted_for_anthropic_when_flagged():
    caps = _anthropic_web_search_capabilities("anthropic:claude-opus-4-8", web=True)
    from pydantic_ai.capabilities import WebSearch

    assert caps is not None and len(caps) == 1
    assert isinstance(caps[0], WebSearch)
    # The native (server-side) Anthropic web_search tool, never a local fallback.
    assert caps[0].native is not None and caps[0].native.kind == "web_search"
    assert caps[0].local is None


def test_web_capability_gated_off_everywhere_else():
    # Non-anthropic providers never get the anthropic server tool.
    assert _anthropic_web_search_capabilities("openai:gpt-4o", web=True) is None
    assert _anthropic_web_search_capabilities("google-gla:gemini-2.5-flash", web=True) is None
    # An unflagged request never gets it, even on anthropic.
    assert _anthropic_web_search_capabilities("anthropic:claude-opus-4-8", web=False) is None
    # The model_override sentinel ("" resolved string) never gets it.
    assert _anthropic_web_search_capabilities("", web=True) is None


def test_run_request_web_defaults_false():
    req = RunRequest(system_prompt="s", instructions="i", config=LLMConfig(model="m"))
    assert req.web is False


# ── runner threading: the flag reaches (only) the flagged anthropic Agent build ─


class _CaptureAgent:
    """Stands in for ``pydantic_ai.Agent`` via the ``_import_pydantic_ai`` seam:
    records the construction kwargs and returns a canned text result — no model,
    no network."""

    captured: list[dict] = []

    def __init__(self, model, **kwargs):
        _CaptureAgent.captured.append({"model": model, "kwargs": kwargs})

    def run_sync(self, prompt, usage_limits=None):
        return SimpleNamespace(output="ok", usage=SimpleNamespace(), response=None)


def _agent_kwargs(monkeypatch, tmp_path, *, model: str, web: bool) -> dict:
    """Run a single-turn text request through PydanticAIRunner with the Agent class
    swapped for the capture double; return the Agent construction kwargs."""
    import rebar.llm.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", lambda: _CaptureAgent)
    # The real anthropic-path model construction needs a key present (never called).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    cfg = LLMConfig(model=model, repo_path=str(tmp_path))
    req = RunRequest(
        system_prompt="s",
        instructions="i",
        config=cfg,
        reviewers=["plan-reviewer"],
        mode="text",
        execution_mode="single_turn",
        web=web,
    )
    before = len(_CaptureAgent.captured)
    PydanticAIRunner(cfg).run(req)
    assert len(_CaptureAgent.captured) == before + 1
    return _CaptureAgent.captured[-1]["kwargs"]


def test_runner_attaches_web_capability_on_flagged_anthropic(monkeypatch, tmp_path):
    kwargs = _agent_kwargs(monkeypatch, tmp_path, model="anthropic:claude-opus-4-8", web=True)
    from pydantic_ai.capabilities import WebSearch

    caps = kwargs.get("capabilities")
    assert caps is not None and len(caps) == 1 and isinstance(caps[0], WebSearch)
    assert caps[0].native.kind == "web_search"


def test_runner_unflagged_anthropic_settings_byte_identical(monkeypatch, tmp_path):
    flagged = _agent_kwargs(monkeypatch, tmp_path, model="anthropic:claude-opus-4-8", web=True)
    unflagged = _agent_kwargs(monkeypatch, tmp_path, model="anthropic:claude-opus-4-8", web=False)
    assert "capabilities" not in unflagged
    # The ONLY delta the flag introduces is the capabilities entry.
    assert unflagged == {k: v for k, v in flagged.items() if k != "capabilities"}


def test_runner_non_anthropic_settings_byte_identical(monkeypatch, tmp_path):
    for model in ("openai:gpt-4o", "google-gla:gemini-2.5-flash"):
        flagged = _agent_kwargs(monkeypatch, tmp_path, model=model, web=True)
        unflagged = _agent_kwargs(monkeypatch, tmp_path, model=model, web=False)
        assert "capabilities" not in flagged
        assert flagged == unflagged


# ── routing: the packaged T1 entry declares web; pass1 threads it per criterion ─


def _packaged_routing() -> dict:
    text = resources.files("rebar.llm").joinpath("plan_review/criteria_routing.json").read_text()
    return json.loads(text)


def test_packaged_routing_declares_web_on_t1_only():
    routing = _packaged_routing()
    assert routing["T1"].get("web") is True
    assert [cid for cid, e in routing.items() if e.get("web")] == ["T1"]


class _CaptureRunner:
    """Runner double that records each RunRequest and returns an empty findings set."""

    name = "capture"

    def __init__(self):
        self.reqs: list[RunRequest] = []

    def preflight(self) -> None:  # pragma: no cover — protocol completeness
        pass

    def run(self, req: RunRequest) -> dict:
        self.reqs.append(req)
        return {"findings": [], "_usage": {}}


def test_pass1_chunk_threads_web_from_criterion_routing():
    from rebar.llm.plan_review import passes, registry

    crits = {c["id"]: c for c in registry.load_criteria(repo_root=None)}
    assert crits["T1"].get("web") is True  # descriptor carries the routing key
    cfg = LLMConfig(model="claude-opus-4-8", repo_path=".")
    r = _CaptureRunner()
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[crits["T1"]], agentic=True)
    assert r.reqs[-1].web is True
    # Another AGENT-tier criterion without the routing key never carries the flag.
    other = crits["G1G2"]
    assert not other.get("web")
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[other], agentic=True)
    assert r.reqs[-1].web is False
    # A single-turn chunk containing T1 (defensive: web rides only the agent tier).
    passes.pass1_chunk(r, cfg, plan="a plan", chunk=[crits["T1"]], agentic=False)
    assert r.reqs[-1].web is False


# ── rubric: T1 states conditional availability + the fallback ──────────────────


def test_t1_rubric_describes_conditional_web_and_fallback():
    text = resources.files("rebar.llm").joinpath("reviewers/plan_review_T1.md").read_text().lower()
    assert "web search" in text or "web-search" in text
    # Conditional availability: the tool applies only when offered on this run.
    assert "when" in text and ("offered" in text or "available" in text)
    # Fallback: codebase/plan reasoning when no web tool is present.
    assert "fall back" in text or "fallback" in text
