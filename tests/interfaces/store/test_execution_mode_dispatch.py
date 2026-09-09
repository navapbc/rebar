"""Story 4b2f: single_turn dispatch end-to-end, OFFLINE via FakeRunner.

A workflow agent step whose prompt is ``execution_mode: single_turn`` runs ONE
structured call validated against the PROMPT's ``outputs`` contract (NOT the step's
mode/output_schema). Exercised through the real executor + RunnerAgentStep bridge
with an injected FakeRunner, so it is fully offline (no tokens, no network).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm import anthropic_model as anthropic_model_mod
from rebar.llm import structured_run as structured_run_mod
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
    # The workflow defaults to an attested HEAD snapshot, so its project prompt must be
    # committed. An uncommitted prompt would correctly be invisible to the runner.
    subprocess.run(
        ["git", "add", ".rebar/prompts/single-shot.md"], cwd=r, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add test prompt"],
        cwd=r,
        check=True,
        capture_output=True,
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
    """Require single-turn requests to build an agent with no tools or toolsets."""
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

    monkeypatch.setattr(structured_run_mod, "_pai_structured", _fake_structured)
    # Stub caching at its capability seam to avoid provider settings imports through the
    # empty ``pydantic_ai`` double. Accept the required keyword-only execution mode.
    monkeypatch.setattr(runner_mod, "cache_settings_for", lambda caps, *, execution_mode: None)
    monkeypatch.setattr(structured_run_mod, "_import_pydantic_ai", lambda: object)
    monkeypatch.setattr(anthropic_model_mod, "_pai_model", lambda cfg: "anthropic:fake")
    # Disable the ambient loopback-proxy bypass; it imports the real Anthropic model against
    # the empty ``pydantic_ai`` stub.
    monkeypatch.setattr(anthropic_model_mod, "_local_proxy_bypass_base_url", lambda: None)

    # Stub ``ProviderSession`` and select its lazy model-string path so provider construction
    # cannot obscure the no-tools assertion.
    class _NoBuildProviderSession:
        def __init__(self, _cfg):
            pass

        def supports(self, _name):
            return False

        def is_resolvable(self, _name):
            return True

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    monkeypatch.setattr(runner_mod, "ProviderSession", _NoBuildProviderSession)
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
