"""Sampling-temperature plumbing (upstream review-code report §2 — Pass-2 non-determinism).

`LLMConfig.temperature` defaults to None (provider default — byte-unchanged for every existing
caller); when set it rides into the model call's ``model_settings``. A per-request override on
``RunRequest.config`` wins over the runner's own cfg (mirrors the max_tokens seam). The workflow
step layer honors a ``with: temperature`` input so the Pass-2 verify steps can pin greedy (0)
sampling. These are offline (no billable call): a spy captures the Agent's model_settings.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import LLMConfig

pytestmark = pytest.mark.unit


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


# ── Config resolution ─────────────────────────────────────────────────────────
def test_temperature_config_default_is_none():
    """Unset ⇒ None ⇒ NO temperature is sent, so the provider default is used unchanged."""
    assert LLMConfig(repo_path=".").temperature is None


def test_temperature_config_env_override(monkeypatch):
    monkeypatch.setenv("REBAR_LLM_TEMPERATURE", "0")
    assert LLMConfig.from_env(repo_root=".").temperature == 0.0


def test_temperature_config_env_unparseable_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("REBAR_LLM_TEMPERATURE", "not-a-float")
    assert LLMConfig.from_env(repo_root=".").temperature is None


# ── The runner wires temperature into model_settings (via a spy) ──────────────
def _capture_model_settings(cfg, req_cfg=None):
    """Run a text request through the runner with an Agent spy; return the model_settings dict the
    Agent was constructed with (None if none were passed)."""
    import rebar.llm.runner as runner_mod
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}
    real_import = runner_mod._import_pydantic_ai

    def _spy_import():
        RealAgent = real_import()

        class _SpyAgent(RealAgent):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                captured["model_settings"] = kwargs.get("model_settings")
                super().__init__(*args, **kwargs)

        return _SpyAgent

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(runner_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    try:
        req = RunRequest(
            system_prompt="s",
            instructions="i",
            config=req_cfg if req_cfg is not None else cfg,
            reviewers=["v"],
            mode="text",
        )
        PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(req)
    finally:
        mp.undo()
    return captured["model_settings"]


def test_runner_omits_temperature_when_none():
    """cfg.temperature None ⇒ no 'temperature' key in model_settings — the unchanged default."""
    ms = _capture_model_settings(_cfg())
    assert ms is None or "temperature" not in ms


def test_runner_sets_temperature_on_model_settings():
    """cfg.temperature=0 ⇒ model_settings carries temperature 0.0 (greedy)."""
    ms = _capture_model_settings(_cfg(temperature=0.0))
    assert ms is not None and ms["temperature"] == 0.0


def test_request_config_temperature_overrides_runner_cfg():
    """A per-request RunRequest.config.temperature wins over the runner's own cfg."""
    runner_cfg = _cfg(temperature=None)
    req_cfg = _cfg(temperature=0.0)
    ms = _capture_model_settings(runner_cfg, req_cfg=req_cfg)
    assert ms is not None and ms["temperature"] == 0.0


# ── The workflow step layer honors `with: temperature` ────────────────────────
def test_workflow_step_temperature_input_tunes_cfg(monkeypatch):
    """A step carrying `with: {temperature: 0}` builds its RunRequest under a cfg pinned to 0 —
    the seam the Pass-2 verify steps use. Captured by spying the runner the step drives."""
    from rebar.llm.workflow.executor import StepContext
    from rebar.llm.workflow.runs import RunnerAgentStep

    captured: dict = {}

    class _SpyRunner:
        name = "spy"

        def run(self, req):
            captured["temperature"] = req.config.temperature
            return {"findings": []}

    step = RunnerAgentStep(runner=_SpyRunner(), repo_root=".")
    ctx = StepContext(
        run_id="r",
        step_id="verify",
        kind="agent",
        step={"prompt": "plan-review-verifier", "mode": "structured"},
        inputs={"ticket_id": "T-1", "plan": "P", "instructions": "i", "temperature": 0},
        workflow={},
        target_ticket="T-1",
        repo_root=".",
    )
    step.run(ctx)
    assert captured["temperature"] == 0.0
