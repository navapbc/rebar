"""``build_model_settings`` / ``build_usage_limits`` — the settings block lifted out of
``PydanticAIRunner.run()`` (task 8229, ADR 0056 decision 3).

Two contracts are easy to break silently in this move and are asserted here:

* **Purity.** ``build_model_settings`` decides whether to WITHDRAW ``temperature`` for a model
  measured to reject it, but the once-per-model dedup logging (``_TEMPERATURE_WITHDRAWN_LOGGED``)
  must STAY in ``run()``. That set lives in ``runner.py`` while the builder lives in the LEAF
  module ``structured_run.py``, which may not import ``runner`` at runtime — a rule enforced by
  ``test_structured_run_seam.py::test_structured_run_has_no_runtime_import_from_runner``.
* **The returned counters.** ``build_usage_limits`` must hand back ``eff_max_iter`` explicitly.
  It is NOT recoverable from the ``UsageLimits`` it builds: only ``max(8, eff_max_iter)`` is
  stored there as ``tool_calls_limit``, and ``max()`` is not invertible — for any
  ``eff_max_iter <= 8`` the original value is gone. ``run()`` needs it for ``FailureContext``
  and the telemetry log.
"""

from __future__ import annotations

import pytest

from rebar.llm import anthropic_model as anthropic_model_mod
from rebar.llm import structured_run as structured_run_mod

pytest.importorskip("pydantic_ai")

from pydantic_ai.usage import UsageLimits

from rebar.llm import structured as _structured
from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest, _effective_config
from rebar.llm.structured_run import build_model_settings, build_usage_limits

pytestmark = pytest.mark.unit

_MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"


def _caps(*, supports_temperature: bool = True) -> ModelCapabilities:
    return ModelCapabilities(
        native_structured_output=True,
        prompt_cache_style="none",
        supports_thinking=False,
        supports_temperature=supports_temperature,
    )


def _req(cfg: LLMConfig, **kw) -> RunRequest:
    return RunRequest(system_prompt="s", instructions="i", config=cfg, reviewers=["v"], **kw)


def _cfg(**kw) -> LLMConfig:
    return LLMConfig(repo_path=".", model=_MODEL, **kw)


# ── §A happy path ────────────────────────────────────────────────────────────────────────


def test_model_settings_start_from_the_cache_settings_and_carry_the_knobs():
    """The base dict is the resolved cache settings; the operator's caps ride alongside."""
    cfg = _cfg(max_tokens=8000, timeout_s=42, temperature=0.0)
    out = build_model_settings(
        cfg,
        _req(cfg),
        _caps(),
        _MODEL,
        {"anthropic_cache_instructions": True},
        model_override=None,
    )
    assert out["anthropic_cache_instructions"] is True, "cache settings must be the base"
    assert out["max_tokens"] == 8000
    assert out["timeout"] == 42.0
    assert out["temperature"] == 0.0


def test_usage_limits_returns_the_limits_object_and_both_counters():
    """The caller unpacks three values; `req_limit` is ~one model request per tool-call cycle."""
    cfg = _cfg(max_iterations=10)
    limits, req_limit, eff_max_iter = build_usage_limits(cfg, _req(cfg), UsageLimits)
    assert isinstance(limits, UsageLimits)
    assert eff_max_iter == 10
    assert req_limit == 5, "the RETURNED req_limit stays the bare base (banking inverse)"
    assert limits.request_limit == 5 + _structured.OUTPUT_RETRIES, (
        "the CONSTRUCTED request_limit adds the RP-01 S2 output-retry allowance, so the "
        "request budget can never trip before the output-retry counter"
    )


# ── §B the temperature-withdrawal matrix, and the PURITY it must not break ───────────────


def test_temperature_is_withdrawn_for_a_model_measured_to_reject_it():
    """The negative half of the matrix. Without this, a 'withdraw always' implementation and a
    'never withdraw' implementation both pass the positive case."""
    cfg = _cfg(temperature=0.0)
    out = build_model_settings(
        cfg, _req(cfg), _caps(supports_temperature=False), _MODEL, None, model_override=None
    )
    assert "temperature" not in out, (
        "temperature was sent to a model whose capabilities say it 400s on it"
    )


def test_temperature_is_retained_when_the_model_supports_it():
    """The positive control that stops a blanket withdrawal passing — losing Pass-2 greedy
    determinism for every model."""
    cfg = _cfg(temperature=0.0)
    out = build_model_settings(
        cfg, _req(cfg), _caps(supports_temperature=True), _MODEL, None, model_override=None
    )
    assert out["temperature"] == 0.0


def test_building_settings_has_no_side_effect_on_the_dedup_set():
    """THE LEAF INVARIANT. The once-per-model log dedup set lives in runner.py; the builder lives
    in structured_run.py, which may not import runner at runtime. An implementation that moved
    the `.add()` along with the decision would need that import (or would relocate the set and
    break runner's own reference)."""
    from rebar.llm import runner as runner_mod

    runner_mod._TEMPERATURE_WITHDRAWN_LOGGED.clear()
    cfg = _cfg(temperature=0.0)
    build_model_settings(
        cfg, _req(cfg), _caps(supports_temperature=False), _MODEL, None, model_override=None
    )
    assert runner_mod._TEMPERATURE_WITHDRAWN_LOGGED == set(), (
        "build_model_settings mutated runner's dedup set — the side effect must stay in run()"
    )


def test_an_unset_temperature_is_never_sent():
    """Both cfg and request leave it None by default, so the provider default is used and the
    call stays byte-unchanged from before the temperature seam existed."""
    cfg = _cfg()
    out = build_model_settings(cfg, _req(cfg), _caps(), _MODEL, None, model_override=None)
    assert "temperature" not in out


# ── §C per-request overrides may only RAISE the operator floor ───────────────────────────


def test_a_per_request_output_cap_raises_but_never_lowers_the_floor():
    cfg = _cfg(max_tokens=8000)
    raised = build_model_settings(
        cfg, _req(_cfg(max_tokens=20000)), _caps(), _MODEL, None, model_override=None
    )
    assert raised["max_tokens"] == 20000, "a request may RAISE the output cap"
    lowered = build_model_settings(
        cfg, _req(_cfg(max_tokens=100)), _caps(), _MODEL, None, model_override=None
    )
    assert lowered["max_tokens"] == 8000, "a request must NOT lower the operator floor"


def test_an_explicit_output_token_limit_clamps_with_a_floor_of_256():
    """`output_token_limit` is the one knob that may reduce the cap — bounded below at 256 so a
    caller cannot starve the response to nothing."""
    cfg = _cfg(max_tokens=8000)
    out = build_model_settings(
        cfg, _req(cfg, output_token_limit=1000), _caps(), _MODEL, None, model_override=None
    )
    assert out["max_tokens"] == 1000
    starved = build_model_settings(
        cfg, _req(cfg, output_token_limit=1), _caps(), _MODEL, None, model_override=None
    )
    assert starved["max_tokens"] == 256, "the clamp must not go below the 256 floor"


def test_a_per_request_step_budget_raises_but_never_lowers():
    cfg = _cfg(max_iterations=10)
    _, _, raised = build_usage_limits(cfg, _req(_cfg(max_iterations=40)), UsageLimits)
    assert raised == 40
    _, _, lowered = build_usage_limits(cfg, _req(_cfg(max_iterations=2)), UsageLimits)
    assert lowered == 10, "a request must NOT lower the operator-configured step floor"


def test_an_explicit_iteration_limit_clamps_with_a_floor_of_2():
    cfg = _cfg(max_iterations=40)
    _, _, clamped = build_usage_limits(cfg, _req(cfg, iteration_limit=6), UsageLimits)
    assert clamped == 6
    _, _, floored = build_usage_limits(cfg, _req(cfg, iteration_limit=1), UsageLimits)
    assert floored == 2, "the clamp must not go below the 2-step floor"


# ── §D the counters the UsageLimits object cannot give back ──────────────────────────────


def test_eff_max_iter_is_returned_even_when_the_limits_object_cannot_encode_it():
    """`tool_calls_limit` stores max(8, eff_max_iter). For eff_max_iter <= 8 that is 8 for EVERY
    input, so the caller cannot invert it — which is exactly why the value is returned. An
    implementation that dropped the third element and let run() read the object back would be
    wrong here and nowhere else."""
    cfg = _cfg(max_iterations=4)
    limits, req_limit, eff_max_iter = build_usage_limits(cfg, _req(cfg), UsageLimits)
    assert eff_max_iter == 4, "the true step budget must survive the round trip"
    assert limits.tool_calls_limit == 8, "the object floors it at 8, losing the original"
    assert req_limit == 2


def test_request_limit_is_at_least_one_for_a_tiny_budget():
    """ceil(1/2) == 1; a zero request_limit would make every call fail before it started."""
    cfg = _cfg(max_iterations=1)
    _, req_limit, _ = build_usage_limits(cfg, _req(cfg), UsageLimits)
    assert req_limit >= 1


# ── §E the returned dict must not alias the caller's cache settings ──────────────────────


def test_the_returned_settings_do_not_alias_the_cache_settings_input():
    """`dict(cache_settings)` is a COPY. Aliasing would let one call's max_tokens leak into the
    shared cache-settings dict and thus into every later call."""
    shared = {"anthropic_cache_instructions": True}
    cfg = _cfg(max_tokens=8000)
    out = build_model_settings(cfg, _req(cfg), _caps(), _MODEL, shared, model_override=None)
    assert "max_tokens" in out
    assert shared == {"anthropic_cache_instructions": True}, (
        "the builder mutated the caller's cache-settings dict"
    )


def test_absent_cache_settings_yield_a_plain_dict():
    cfg = _cfg(timeout_s=30)
    out = build_model_settings(cfg, _req(cfg), _caps(), _MODEL, None, model_override=None)
    assert out["timeout"] == 30.0


def test_request_limits_only_lower_transport_policy() -> None:
    """Request-local limits lower the run-wide policy; omitted limits preserve it."""
    base = _cfg(timeout_s=600, llm_retry_max_attempts=4)
    unchanged = _effective_config(base, _req(base))
    assert unchanged is base

    bounded_req = _req(base)
    bounded_req.request_timeout_limit_s = 60
    bounded_req.transport_attempt_limit = 1
    bounded = _effective_config(base, bounded_req)
    assert bounded.timeout_s == 60
    assert bounded.llm_retry_max_attempts == 1
    assert base.timeout_s == 600
    assert base.llm_retry_max_attempts == 4

    raised_req = _req(base)
    raised_req.request_timeout_limit_s = 1200
    raised_req.transport_attempt_limit = 8
    raised = _effective_config(base, raised_req)
    assert raised.timeout_s == 600
    assert raised.llm_retry_max_attempts == 4


def test_request_transport_limits_reach_model_and_provider_construction(monkeypatch) -> None:
    """The lowered config exists before either model or transport construction begins."""
    from rebar.llm import runner as runner_mod

    captured: dict[str, LLMConfig] = {}

    class _StopConstruction(Exception):
        pass

    class _CapturingProviderSession:
        def __init__(self, cfg):
            captured["provider"] = cfg

        def __enter__(self):
            raise _StopConstruction

        def __exit__(self, *args):
            return False

    def _capture_model(cfg):
        captured["model"] = cfg
        return _MODEL

    monkeypatch.setattr(structured_run_mod, "_pai_check_config", lambda cfg: None)
    monkeypatch.setattr(anthropic_model_mod, "_pai_model", _capture_model)
    monkeypatch.setattr(runner_mod, "primary_endpoint_for", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_mod, "ProviderSession", _CapturingProviderSession)

    base = _cfg(timeout_s=600, llm_retry_max_attempts=4)
    req = _req(base, mode="text", execution_mode="single_turn")
    req.request_timeout_limit_s = 60
    req.transport_attempt_limit = 1
    with pytest.raises(_StopConstruction):
        PydanticAIRunner(base).run(req)

    assert captured["model"].timeout_s == 60
    assert captured["model"].llm_retry_max_attempts == 1
    assert captured["provider"].timeout_s == 60
    assert captured["provider"].llm_retry_max_attempts == 1
