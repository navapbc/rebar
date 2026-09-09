"""Per-request model selection through ``RunRequest.config`` (story b690).

The runner's shared configuration is the floor. A workflow step may override it without mutating
other steps. Tests observe the model delivered to ``_pai_model``, the selection point feeding the
provider session, capabilities, fallback chain, and caching decision, rather than merely checking
the request value that the runner could ignore.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rebar.llm import anthropic_model as anthropic_model_mod

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import LLMConfig

pytestmark = pytest.mark.unit

_FLOOR_MODEL = "anthropic:claude-opus-4-8"
_PER_CALL_MODEL = "anthropic:claude-sonnet-4-6"


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    kw.setdefault("model", _FLOOR_MODEL)
    return LLMConfig(**kw)


def _resolved_model(runner_cfg: LLMConfig, req_cfg: LLMConfig | None = None) -> str:
    """Run one text request offline; return the model string the runner actually resolved.

    ``_pai_model`` is spied rather than replaced — it records and then delegates — so resolution
    keeps its real behaviour and the run proceeds to completion instead of aborting at the probe.
    """
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    seen: list[str] = []
    real_pai_model = anthropic_model_mod._pai_model

    def _spy(cfg):
        seen.append(cfg.model)
        return real_pai_model(cfg)

    mp = pytest.MonkeyPatch()
    mp.setattr(anthropic_model_mod, "_pai_model", _spy)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def gen(messages, info):
        return ModelResponse(parts=[TextPart("hi")])

    try:
        req = RunRequest(
            system_prompt="s",
            instructions="i",
            config=req_cfg if req_cfg is not None else runner_cfg,
            reviewers=["v"],
            mode="text",
        )
        PydanticAIRunner(runner_cfg, model_override=FunctionModel(gen)).run(req)
    finally:
        mp.undo()
    assert seen, "the runner never resolved a model — the probe did not observe the call path"
    return seen[0]


def test_request_config_model_overrides_the_runners_own_cfg():
    """THE DEFECT. A per-request model must win over the shared runner config."""
    assert _resolved_model(_cfg(), req_cfg=_cfg(model=_PER_CALL_MODEL)) == _PER_CALL_MODEL, (
        "the runner resolved its own cfg.model and discarded the per-request model"
    )


def test_runner_cfg_model_is_used_when_the_request_carries_the_same_one():
    """The control: with no divergence the resolved model is unchanged, so the test above cannot
    pass merely because SOME model reached the resolver."""
    assert _resolved_model(_cfg()) == _FLOOR_MODEL


def test_a_per_request_model_does_not_mutate_the_shared_runner_config():
    """The reason this is an override and not an assignment: the runner is shared across every
    step of a gate run, so honouring one step's model must not change the floor for the next.
    """
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    runner_cfg = _cfg()
    runner = PydanticAIRunner(
        runner_cfg, model_override=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("hi")]))
    )

    seen: list[str] = []
    real_pai_model = anthropic_model_mod._pai_model
    mp = pytest.MonkeyPatch()
    mp.setattr(
        anthropic_model_mod,
        "_pai_model",
        lambda cfg: (seen.append(cfg.model), real_pai_model(cfg))[1],
    )
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    try:
        base = RunRequest(system_prompt="s", instructions="i", config=runner_cfg, mode="text")
        runner.run(replace(base, config=_cfg(model=_PER_CALL_MODEL)))
        runner.run(base)  # the NEXT step, which declared nothing
    finally:
        mp.undo()

    assert seen == [_PER_CALL_MODEL, _FLOOR_MODEL], (
        f"a per-request model leaked into the shared runner config and changed a later step: {seen}"
    )
    assert runner_cfg.model == _FLOOR_MODEL, "the runner's own config object was mutated"
