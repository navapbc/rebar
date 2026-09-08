"""Pydantic output-policy contracts.

The output function delegates to deterministic parsing, translates retryable errors
to ``ModelRetry`` with the exact cause, and propagates terminal errors unchanged. The
guard rejects terminal response metadata before text parsing and translates transient
errors for bounded Pydantic retry. Offline tests also forbid scheduling, provider,
usage, or persistence coupling.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from pydantic import BaseModel, field_validator
from pydantic_ai import Agent, ModelRetry, TextOutput
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm import structured
from rebar.llm.errors import StructuredOutputError, UnretryableOutputError

pytestmark = pytest.mark.unit


class _Verdict(BaseModel):
    verdict: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _bound(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No live/billable call can escape this module."""
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _agent(gen, *, retries: int = 0):
    """A real Agent wired with the adapter's output function + guard capability."""
    from rebar.llm import pai_output

    return Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": retries} if retries else None,
    )


def _emit(*texts, finish_reason=None, provider_details=None):
    """Build a FunctionModel `gen` that emits `texts` in order and counts calls."""
    state = {"n": 0}

    def gen(messages, info):
        i = state["n"]
        state["n"] += 1
        text = texts[min(i, len(texts) - 1)]
        return ModelResponse(
            parts=[TextPart(text)],
            finish_reason=finish_reason,
            provider_details=provider_details,
        )

    gen.calls = state
    return gen


def _retry_prompt_present(result) -> bool:
    return any(
        "Retry" in type(p).__name__ for m in result.all_messages() for p in getattr(m, "parts", [])
    )


# Happy-path typed-output contract.


def test_agent_returns_typed_object_for_valid_native_json():
    """A Pydantic Agent returns the typed object for valid native JSON."""
    gen = _emit('{"verdict": "PASS", "confidence": 0.9}')
    result = _agent(gen).run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS", confidence=0.9)
    assert gen.calls["n"] == 1


def test_agent_returns_typed_object_for_prompted_freetext():
    """Prose-wrapped or fenced output follows tolerant parsing to the typed object."""
    gen = _emit('Sure! Here it is:\n```json\n{"verdict": "FAIL"}\n```\nHope that helps.')
    result = _agent(gen).run_sync("evaluate")
    assert result.output == _Verdict(verdict="FAIL", confidence=1.0)


# Held-out repair, sentinel, and decoy corpus. Adapter and pure parser must agree.
_CORPUS = {
    "strict": '{"verdict": "PASS"}',
    "markdown_fence": '```json\n{"verdict": "PASS"}\n```',
    "trailing_comma": '{"verdict": "PASS",}',
    "unclosed_brace": '{"verdict": "PASS"',
    "single_quotes": "{'verdict': 'PASS'}",
    "prose_wrapped": 'Sure! Here is the result: {"verdict": "PASS"} — hope that helps.',
    "sentinel": ('prose before\n<<<REBAR_OUTPUT>>>\n{"verdict": "PASS"}\n<<<END>>>\nprose after'),
    "decoy_then_valid": ('{"relation": "depends_on"} then the real answer {"verdict": "PASS"}'),
}


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_output_function_matches_pure_parser_on_corpus(name):
    """Preserve corpus selection against independent expectations and the pure parser."""
    from rebar.llm import pai_output

    text = _CORPUS[name]
    got = pai_output.output_function(_Verdict)(text)
    assert got == _Verdict(verdict="PASS")
    assert got == structured.parse_structured(text, _Verdict)


def test_retryable_validation_failure_preserves_exact_error_in_cause(monkeypatch):
    """ModelRetry preserves the exact retryable parse error as its cause."""
    from rebar.llm import pai_output

    sentinel = StructuredOutputError("bespoke validation failure XYZ")

    def _boom(text, model_cls):
        raise sentinel

    monkeypatch.setattr(structured, "parse_structured", _boom)
    with pytest.raises(ModelRetry) as excinfo:
        pai_output.output_function(_Verdict)("anything")
    assert excinfo.value.__cause__ is sentinel


def test_output_function_does_not_wrap_unretryable_in_modelretry(monkeypatch):
    """A terminal parse error propagates unchanged, never as ModelRetry."""
    from rebar.llm import pai_output

    terminal = UnretryableOutputError("refused/truncated")

    def _boom(text, model_cls):
        raise terminal

    monkeypatch.setattr(structured, "parse_structured", _boom)
    with pytest.raises(UnretryableOutputError) as excinfo:
        pai_output.output_function(_Verdict)("anything")
    assert excinfo.value is terminal


def test_invalid_then_valid_drives_pydantic_bounded_retry(monkeypatch):
    """Invalid then valid output uses bounded retry and emits a retry prompt."""
    gen = _emit("not json at all", '{"verdict": "PASS"}')
    result = _agent(gen, retries=2).run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS")
    assert gen.calls["n"] == 2
    assert _retry_prompt_present(result)


def test_refusal_finish_reason_is_terminal_without_retry_prompt():
    """A refusal is terminal after one call and emits no retry prompt."""
    from rebar.llm import pai_output

    gen = _emit('{"verdict": "PASS"}', finish_reason="content_filter")
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_truncation_length_is_terminal_one_call():
    """A length truncation is terminal after one call, without retry."""
    from rebar.llm import pai_output

    gen = _emit('{"verdict": "PA', finish_reason="length")
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_provider_details_refusal_defense_in_depth():
    """A refusal reported only in provider_details is still terminal."""
    from rebar.llm import pai_output

    gen = _emit(
        '{"verdict": "PASS"}',
        finish_reason="stop",
        provider_details={"finish_reason": "refusal", "refusal": "policy"},
    )
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_transient_error_finish_reason_retries_not_aborts():
    """A transient finish error retries once, then returns the clean typed output."""
    from rebar.llm import pai_output

    state = {"n": 0}

    def gen(messages, info):
        state["n"] += 1
        finish_reason = "error" if state["n"] == 1 else "stop"
        return ModelResponse(parts=[TextPart('{"verdict": "PASS"}')], finish_reason=finish_reason)

    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    result = agent.run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS")
    assert state["n"] == 2


def test_guard_retryable_error_preserves_exact_cause(monkeypatch):
    """The guard's ModelRetry preserves the exact retryable error as ``__cause__``."""
    import asyncio

    from pydantic_ai import ModelRetry

    from rebar.llm import pai_output

    sentinel = StructuredOutputError("transient provider error QYZ")

    def _boom(response):
        raise sentinel

    monkeypatch.setattr(structured, "check_response", _boom)
    guard = pai_output.guard_capability()
    with pytest.raises(ModelRetry) as excinfo:
        asyncio.run(guard.after_model_request(None, request_context=None, response=object()))
    assert excinfo.value.__cause__ is sentinel


def test_guard_unretryable_error_is_not_wrapped(monkeypatch):
    """The guard propagates terminal UnretryableOutputError unchanged."""
    import asyncio

    from rebar.llm import pai_output

    terminal = UnretryableOutputError("refused/truncated")

    def _boom(response):
        raise terminal

    monkeypatch.setattr(structured, "check_response", _boom)
    guard = pai_output.guard_capability()
    with pytest.raises(UnretryableOutputError) as excinfo:
        asyncio.run(guard.after_model_request(None, request_context=None, response=object()))
    assert excinfo.value is terminal


def test_pai_output_adds_no_scheduling_usage_or_persistence():
    """Keep pai_output independent of scheduling, providers, usage, and persistence."""
    import ast
    from pathlib import Path

    from rebar.llm import pai_output

    src = Path(pai_output.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = {
        "rebar.llm.usage_log",
        "rebar.llm.structured_run",
        "rebar.llm.runner",
        "rebar.llm.agent_call",
    }
    assert not (imported & forbidden), f"pai_output must not import {imported & forbidden}"
