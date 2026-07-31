"""Plan-review output-token budgets ride at the resolved model's MAXIMUM (bug 30a2).

Every plan-review LLM call previously ran at the flat ``DEFAULT_MAX_TOKENS`` (16000)
output ceiling, so a finding-rich structured response (e.g. a container bin over many
children under the S7 exhaustiveness directive) truncated and raised
``UnretryableOutputError`` — degrading the whole review to INDETERMINATE. The operator
rule (mirroring the completion verifier's remediation in ``completion_recovery.py``):
**allow effectively unlimited output** — the API requires a finite ``max_tokens``, so
the resolved model's maximum output capacity IS the unlimited setting; output is
naturally bounded by the model's actual findings, and unspent budget costs nothing.

The FINAL RULE, uniform across plan review: every plan-review request carries
``max_tokens = max(configured floor, model_max_output_tokens(resolved model))``, raised
through the existing per-request seam (``runner.effective_max_tokens`` — a request may
only RAISE the floor; ``req.output_token_limit`` still clamps down where bounded
recovery needs it). Bespoke builders (Pass-1 chunk/AGENT, container, ISF, ISF
summarizer, completion sub-call, coach, prerequisite finder) widen via
``passes._max_output_cfg``; the workflow verify/coach prompt steps opt in via the
``with: {output_budget: model_max}`` input (the agentic verify arm keeps its
``output_tokens_per_item`` scaling as an additional floor-raiser for unmapped models).
"""

from __future__ import annotations

import pytest

from rebar.llm.config import DEFAULT_MAX_TOKENS, LLMConfig
from rebar.llm.errors import UnretryableOutputError
from rebar.llm.plan_review import passes, prerequisites, sizing
from rebar.llm.prompting import prompts
from rebar.llm.workflow.executor import StepContext
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

_SONNET_MAX = 128_000
_HAIKU_MAX = 64_000


class _Recorder:
    """Runner that records every request and returns a canned payload dict."""

    name = "recorder"

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.requests: list = []

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        self.requests.append(req)
        return dict(self._payload)


def _cfg(model: str = "claude-sonnet-4-6") -> LLMConfig:
    return LLMConfig(runner="fake", model=model)


# ── the per-model max-output lookup ───────────────────────────────────────────────


def test_model_max_output_lookup_known_models() -> None:
    assert sizing.model_max_output_tokens("claude-haiku-4-5") == _HAIKU_MAX
    assert sizing.model_max_output_tokens("claude-sonnet-4-6") == _SONNET_MAX
    assert sizing.model_max_output_tokens("claude-opus-4-8") == _SONNET_MAX
    # Provider-qualified strings still match (substring, like largest_window_tokens).
    assert sizing.model_max_output_tokens("anthropic:claude-haiku-4-5") == _HAIKU_MAX


def test_model_max_output_lookup_unknown_falls_back_to_default() -> None:
    assert sizing.model_max_output_tokens("gpt-x-unknown") == DEFAULT_MAX_TOKENS
    assert sizing.model_max_output_tokens(None) == DEFAULT_MAX_TOKENS
    assert sizing.model_max_output_tokens("") == DEFAULT_MAX_TOKENS


def test_max_output_cfg_never_lowers_an_operator_raise() -> None:
    """An operator floor ABOVE the model max is preserved (the seam only raises)."""
    cfg = LLMConfig(runner="fake", model="claude-haiku-4-5", max_tokens=200_000)
    assert passes._max_output_cfg(cfg).max_tokens == 200_000


# ── every bespoke plan-review request builder carries the model-max budget ────────


def _request_max_tokens(recorder: _Recorder) -> list[int]:
    return [req.config.max_tokens for req in recorder.requests]


def test_pass1_chunk_carries_model_max_output() -> None:
    runner = _Recorder({"findings": []})
    passes.pass1_chunk(
        runner,
        _cfg(),
        plan="## Plan\nBuild X.",
        chunk=[{"id": "G6", "title": "t", "detection": "d"}],
    )
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_pass1_chunk_agentic_carries_model_max_output() -> None:
    runner = _Recorder({"findings": []})
    passes.pass1_chunk(
        runner,
        _cfg(),
        plan="## Plan\nBuild X.",
        chunk=[{"id": "T1", "title": "t", "detection": "d"}],
        agentic=True,
    )
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_pass1_container_carries_model_max_output() -> None:
    runner = _Recorder({"findings": []})
    passes.pass1_container(
        runner,
        _cfg(),
        parent_plan="## Plan\nEpic.",
        children=[{"ticket_id": "c1", "title": "child", "description": "d"}],
        criteria=[{"id": "G3", "title": "t", "detection": "d"}],
        sibling_roster="c1: child",
    )
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_pass1_isf_carries_model_max_output() -> None:
    runner = _Recorder({"findings": []})
    passes.pass1_isf(runner, _cfg(), plan="## Plan\nBuild X.", session_log_text="log")
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_summarize_for_isf_carries_model_max_output() -> None:
    runner = _Recorder({"text": "summary"})
    passes.summarize_for_isf(runner, _cfg(), log_text="a long log")
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_pass2_completion_carries_model_max_output() -> None:
    runner = _Recorder({"completions": []})
    passes.pass2_completion(
        runner,
        _cfg(),
        plan="## Plan\nBuild X.",
        findings=[{"finding": "f", "criteria": ["G3"]}],
        delivered_manifest=[{"ticket_id": "c1", "ac": "done"}],
    )
    assert _request_max_tokens(runner) == [_SONNET_MAX]


def test_prerequisite_finder_carries_model_max_output() -> None:
    runner = _Recorder({"records": []})
    prerequisites.run_focused_finder(
        runner,
        _cfg(),
        subject_plan="## Plan\nBuild X.",
        blocks=[{"canonical_id": "p1", "rendered_text": "prereq plan"}],
    )
    assert runner.requests, "the finder made no LLM call"
    assert all(mt == _SONNET_MAX for mt in _request_max_tokens(runner))


def test_unknown_model_builder_falls_back_to_default_without_error() -> None:
    runner = _Recorder({"findings": []})
    passes.pass1_chunk(
        runner,
        _cfg(model="some-future-model"),
        plan="## Plan\nBuild X.",
        chunk=[{"id": "G6", "title": "t", "detection": "d"}],
    )
    assert _request_max_tokens(runner) == [DEFAULT_MAX_TOKENS]


# ── the AC regression fixture: an oversized container completes under the raise ───


class _CeilingEnforcingRunner:
    """Simulates the provider output ceiling: a structured response needing more
    output tokens than the request's ``max_tokens`` truncates → the structured
    stack raises ``UnretryableOutputError`` (the live failure on epic a492's
    17-child bin at the flat 16000 ceiling)."""

    name = "ceiling"
    required_output_tokens = 20_000  # > DEFAULT_MAX_TOKENS, << model max

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        if req.config.max_tokens < self.required_output_tokens:
            raise UnretryableOutputError("response truncated: stop_reason=max_tokens")
        return {"findings": []}


def test_oversized_container_completes_under_model_max_budget() -> None:
    children = [
        {"ticket_id": f"c{i}", "title": f"child {i}", "description": "work " * 50}
        for i in range(17)
    ]
    findings, usage = passes.pass1_container(
        _CeilingEnforcingRunner(),
        _cfg(),
        parent_plan="## Plan\nEpic a492.",
        children=children,
        criteria=[{"id": "G3", "title": "t", "detection": "d"}],
        sibling_roster="roster",
    )
    assert findings == []
    assert usage == {}


# ── the workflow verify/coach steps: `output_budget: model_max` raises the cap ────


class _CapturingRunner(_Recorder):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload)

    @property
    def max_tokens(self) -> int | None:
        return self.requests[-1].config.max_tokens if self.requests else None


def _verify_ctx(inputs: dict) -> StepContext:
    return StepContext(
        run_id="r",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "plan-review-verifier-agentic",
            "mode": "structured",
            "output_schema": "plan_review_verification",
        },
        inputs={
            "ticket_id": "T-1",
            "shared_prefix": prompts.shared_plan_prefix("## Plan\nBuild X in src/x.py."),
            **inputs,
        },
        workflow={"name": "plan-review"},
        target_ticket="T-1",
        repo_root=None,
    )


def test_step_output_budget_model_max_raises_to_model_max(monkeypatch) -> None:
    monkeypatch.setenv("REBAR_LLM_MODEL", "claude-sonnet-4-6")
    runner = _CapturingRunner({"verifications": []})
    ctx = _verify_ctx(
        {
            "findings": [{"finding": "f", "criteria": ["G6"]}],
            "instructions": "verify",
            "output_budget": "model_max",
        }
    )
    res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert res.status == "succeeded"
    assert runner.max_tokens == _SONNET_MAX


def test_step_output_budget_combines_with_per_item_scaling(monkeypatch) -> None:
    """On an unmapped model the per-item scaling still raises the cap — the final
    budget is the max of the floor, the per-item scale, and the model-max lookup."""
    monkeypatch.setenv("REBAR_LLM_MODEL", "some-future-model")
    findings = [{"finding": f"f{i}", "criteria": ["G6"]} for i in range(30)]
    runner = _CapturingRunner({"verifications": []})
    ctx = _verify_ctx(
        {
            "findings": findings,
            "instructions": "verify",
            "output_tokens_per_item": 2000,
            "output_budget": "model_max",
        }
    )
    res = RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert res.status == "succeeded"
    assert runner.max_tokens == 2000 * len(findings)  # scaling wins over the 16000 fallback


def test_step_without_output_budget_keeps_configured_cap() -> None:
    runner = _CapturingRunner({"verifications": []})
    ctx = _verify_ctx({"findings": [], "instructions": "verify"})
    RunnerAgentStep(runner=runner, repo_root=None).run(ctx)
    assert runner.max_tokens == LLMConfig.from_env().max_tokens


def test_plan_review_yaml_states_the_uniform_rule() -> None:
    """Every plan-review LLM prompt step (verify both arms, prerequisite verifier,
    coach both arms) opts into the model-max output budget."""
    from pathlib import Path

    import rebar.llm.workflow as workflow_pkg

    yaml_path = Path(workflow_pkg.__file__).parent / "gates" / "plan-review.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    assert text.count("output_budget: model_max") >= 5
