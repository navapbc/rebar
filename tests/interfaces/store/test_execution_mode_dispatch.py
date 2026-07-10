"""Story 4b2f: single_turn dispatch end-to-end, OFFLINE via FakeRunner.

A workflow agent step whose prompt is ``execution_mode: single_turn`` runs ONE
structured call validated against the PROMPT's ``outputs`` contract (NOT the step's
mode/output_schema). Exercised through the real executor + RunnerAgentStep bridge
with an injected FakeRunner, so it is fully offline (no tokens, no network).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import runs

pytest.importorskip("jsonschema")


def test_single_turn_step_runs_structured_against_prompt_outputs(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    pdir = Path(r) / ".rebar" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "single-shot.md").write_text(
        "---\nexecution_mode: single_turn\noutputs: completion_verdict\n---\n"
        "Decide on {{ticket_id}}.",
        encoding="utf-8",
    )
    tid = rebar.create_ticket("task", "ST", description="body", repo_root=r)
    doc = {
        "schema_version": "1",
        "name": "single_turn_demo",
        "steps": [
            {
                "id": "verify",
                "prompt": "single-shot",
                # A DIFFERENT step mode on purpose: single_turn must OVERRIDE it to
                # structured against the prompt's outputs contract.
                "mode": "findings",
                "with": {"ticket_id": tid, "context": "ctx"},
            }
        ],
    }
    canned = {"verdict": "PASS", "findings": [], "summary": "looks good"}
    res = runs.run(doc, {}, repo_root=r, review_runner=FakeRunner(structured=canned))
    assert res["status"] == "succeeded", res
    out = res["terminal_output"]
    # FakeRunner's structured path validated `canned` against the prompt outputs schema
    # — proving single_turn drove the structured path with the prompt's output_schema.
    assert out["verdict"] == "PASS"
    assert out["summary"] == "looks good"
    assert out["runner"] == "fake"


def test_single_turn_runner_builds_agent_with_no_tools(rebar_repo: Path, monkeypatch) -> None:
    """The no-tools guarantee, asserted directly on PydanticAIRunner.run(): a
    single_turn RunRequest builds the agent with empty tools AND empty toolsets (so it
    is exactly one model call, no tool loop). We stub the heavy pydantic_ai pieces and
    capture the kwargs the runner assembles."""
    from rebar.llm import runner as runner_mod
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}

    # Stub the structured path to capture kwargs without a real model/network call.
    # Returns (payload, usage) — the story-0250 contract.
    def _fake_structured(Agent, model, resolved, req, kwargs, usage_limits):
        captured["tools"] = kwargs.get("tools")
        captured["toolsets"] = kwargs.get("toolsets")
        return {"verdict": "PASS", "findings": [], "summary": "s"}, {}

    monkeypatch.setattr(runner_mod, "_pai_structured", _fake_structured)
    # Caching is orthogonal here; stub it off so we don't import the real anthropic
    # settings module (pydantic_ai is stubbed empty below).
    monkeypatch.setattr(runner_mod, "_anthropic_cache_settings", lambda resolved: None)
    monkeypatch.setattr(runner_mod, "_import_pydantic_ai", lambda: object)
    monkeypatch.setattr(runner_mod, "_pai_model", lambda cfg: "anthropic:fake")
    # Env-independence: the loopback-proxy bypass (commit 4b9e49a57) fires inside run()
    # when ANTHROPIC_BASE_URL is a loopback host and imports the REAL
    # pydantic_ai.models.anthropic — which explodes against the empty pydantic_ai stub
    # below. Stub the bypass off so this test builds the agent regardless of the local
    # ANTHROPIC_BASE_URL (e.g. a dev machine running a headroom proxy on 127.0.0.1).
    monkeypatch.setattr(runner_mod, "_local_proxy_bypass_base_url", lambda: None)
    # story arcticproxy/arcticduck: the runner now wraps ANY anthropic model in the retrying
    # transport (real pydantic_ai import). This test stubs pydantic_ai empty, so stub the
    # builder too — return a (model, http_client) pair without importing the real SDK.
    monkeypatch.setattr(
        runner_mod, "_build_retrying_anthropic_model", lambda *a, **k: ("anthropic:fake", None)
    )
    # finalize_outcome only needs to pass the payload through for this assertion.
    monkeypatch.setattr(
        runner_mod._findings,
        "finalize_outcome",
        lambda outcome, **kw: outcome["structured_response"],
    )
    # Avoid importing the real pydantic_ai submodules / tracing / tools.
    import sys
    import types

    exc_mod = types.ModuleType("pydantic_ai.exceptions")
    exc_mod.UsageLimitExceeded = type("UsageLimitExceeded", (Exception,), {})
    usage_mod = types.ModuleType("pydantic_ai.usage")
    usage_mod.UsageLimits = lambda **kw: object()
    pai_mod = types.ModuleType("pydantic_ai")
    monkeypatch.setitem(sys.modules, "pydantic_ai", pai_mod)
    monkeypatch.setitem(sys.modules, "pydantic_ai.exceptions", exc_mod)
    monkeypatch.setitem(sys.modules, "pydantic_ai.usage", usage_mod)
    tracing = types.ModuleType("rebar.llm.tracing")
    tracing.setup_tracing = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "rebar.llm.tracing", tracing)

    cfg = LLMConfig.from_env(repo_root=str(rebar_repo))
    runner = PydanticAIRunner(cfg)
    req = RunRequest(
        system_prompt="sys",
        instructions="ins",
        config=cfg,
        execution_mode="single_turn",
        mode="structured",
        output_schema="completion_verdict",
    )
    runner.run(req)
    assert captured["tools"] == []
    assert captured["toolsets"] == []
